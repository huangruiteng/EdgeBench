# Copyright (c) 2026 ByteDance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cross-process rolling token admission for host API gateways."""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path

_WINDOW_SECONDS = 60.0


def _process_start(pid: int) -> str | None:
    """Return a Linux process identity that is stable across PID reuse."""
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
        return value.rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, IndexError, OSError):
        return None


def _owner_is_alive(pid: int, process_start: str | None) -> bool:
    current = _process_start(pid)
    if current is not None:
        # When the original start identity was unavailable, fail closed and
        # retain pressure rather than reclaim a potentially live request.
        return process_start is None or current == process_start
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return process_start is None


class TokenAdmissionLedger:
    """Atomically account completed usage and in-flight upper bounds."""

    def __init__(
        self,
        path: str | Path,
        token_limit_per_minute: int,
        max_tokens_per_request: int,
    ) -> None:
        for name, value in (
            ("token_limit_per_minute", token_limit_per_minute),
            ("max_tokens_per_request", max_tokens_per_request),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_tokens_per_request > token_limit_per_minute:
            raise ValueError("max_tokens_per_request must not exceed token_limit_per_minute")
        self.path = Path(path)
        self.token_limit_per_minute = token_limit_per_minute
        self.max_tokens_per_request = max_tokens_per_request
        self.owner_pid = os.getpid()
        self.owner_start = _process_start(self.owner_pid)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_metadata = self.path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
            or parent_metadata.st_mode & 0o022
        ):
            raise ValueError("token ledger parent must be a private owned directory")

        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError("token ledger must not be a symbolic link") from error
            raise

        connection: sqlite3.Connection | None = None
        try:
            descriptor_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(descriptor_metadata.st_mode)
                or descriptor_metadata.st_uid != os.getuid()
            ):
                raise ValueError("token ledger must be an owned regular file")
            os.fchmod(descriptor, 0o600)

            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            try:
                path_metadata = self.path.stat(follow_symlinks=False)
            except FileNotFoundError as error:
                raise ValueError("token ledger changed while opening") from error
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or path_metadata.st_dev != descriptor_metadata.st_dev
                or path_metadata.st_ino != descriptor_metadata.st_ino
            ):
                raise ValueError("token ledger changed while opening")

            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise
        finally:
            os.close(descriptor)

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS token_records ("
                "reservation_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                "owner_pid INTEGER NOT NULL, owner_start TEXT, "
                "reserved_tokens INTEGER NOT NULL, actual_tokens INTEGER, "
                "started_at REAL NOT NULL, accounted_at REAL, "
                "actual_known INTEGER NOT NULL DEFAULT 0)"
            )
            expected = {
                "schema_version": "sforge_token_admission_v1",
                "token_limit_per_minute": str(self.token_limit_per_minute),
                "window_seconds": str(int(_WINDOW_SECONDS)),
            }
            for key, value in expected.items():
                current = connection.execute(
                    "SELECT value FROM metadata WHERE key = ?", (key,)
                ).fetchone()
                if current is None:
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)", (key, value)
                    )
                elif current[0] != value:
                    raise ValueError(f"token ledger {key} does not match this proxy")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _reconcile(connection: sqlite3.Connection, now: float) -> None:
        active = connection.execute(
            "SELECT reservation_id, owner_pid, owner_start, reserved_tokens "
            "FROM token_records WHERE state = 'active'"
        ).fetchall()
        for reservation_id, pid, process_start, reserved_tokens in active:
            if _owner_is_alive(pid, process_start):
                continue
            # A dead owner cannot prove whether the provider used the request.
            # Preserve the full upper bound for a fresh window before reuse.
            connection.execute(
                "UPDATE token_records SET state = 'accounted', actual_tokens = ?, "
                "accounted_at = ?, actual_known = 0 WHERE reservation_id = ?",
                (reserved_tokens, now, reservation_id),
            )
        connection.execute(
            "DELETE FROM token_records WHERE state = 'accounted' AND accounted_at <= ?",
            (now - _WINDOW_SECONDS,),
        )

    def try_reserve(self) -> str | None:
        """Return a reservation id, or None without dispatch when at capacity."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            self._reconcile(connection, now)
            pressure = connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN state = 'active' THEN reserved_tokens "
                "ELSE actual_tokens END), 0) FROM token_records"
            ).fetchone()[0]
            if pressure + self.max_tokens_per_request > self.token_limit_per_minute:
                connection.commit()
                return None
            reservation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO token_records("
                "reservation_id, state, owner_pid, owner_start, reserved_tokens, started_at"
                ") VALUES (?, 'active', ?, ?, ?, ?)",
                (
                    reservation_id,
                    self.owner_pid,
                    self.owner_start,
                    self.max_tokens_per_request,
                    now,
                ),
            )
            connection.commit()
            return reservation_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def settle(self, reservation_id: str, actual_tokens: int | None) -> bool:
        """Replace an in-flight upper bound with observed or conservative usage.

        Returns False when observed usage exceeded the caller-declared maximum.
        The full observed value is still charged so later requests stay fenced.
        """
        if actual_tokens is not None and (
            type(actual_tokens) is not int or actual_tokens < 0
        ):
            raise ValueError("actual_tokens must be a nonnegative integer or None")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT reserved_tokens FROM token_records "
                "WHERE reservation_id = ? AND state = 'active'",
                (reservation_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return True
            reserved_tokens = row[0]
            charged_tokens = reserved_tokens if actual_tokens is None else actual_tokens
            connection.execute(
                "UPDATE token_records SET state = 'accounted', actual_tokens = ?, "
                "accounted_at = ?, actual_known = ? WHERE reservation_id = ?",
                (charged_tokens, time.time(), actual_tokens is not None, reservation_id),
            )
            connection.commit()
            return charged_tokens <= reserved_tokens
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release(self, reservation_id: str) -> None:
        """Release a reservation when no upstream dispatch was attempted."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM token_records "
                "WHERE reservation_id = ? AND state = 'active'",
                (reservation_id,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
