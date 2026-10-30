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

"""Content-free request/response evidence for solver-visible evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "sforge_feedback_transport_v1"
JOURNAL_FILE = "feedback-transport.jsonl"
_LOG_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}$")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def payload_sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_json(value)
    return hashlib.sha256(raw).hexdigest()


class FeedbackTransportRecorder:
    """Append digests and timing without retaining solver or evaluator content."""

    def __init__(self, log_root: Path) -> None:
        self._log_root = log_root
        self._lock = threading.Lock()
        self._sequences: dict[Path, int] = {}

    def _path(self, run_id: str, task_id: str) -> Path:
        if not _LOG_SEGMENT.fullmatch(run_id) or not _LOG_SEGMENT.fullmatch(task_id):
            raise ValueError("Feedback transport run and task IDs must be safe log segments")
        return self._log_root / "runs" / run_id / task_id / JOURNAL_FILE

    def _append(self, path: Path, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            sequence = self._sequences.get(path)
            if sequence is None:
                sequence = sum(1 for line in path.read_bytes().splitlines() if line.strip()) if path.exists() else 0
            sequence += 1
            self._sequences[path] = sequence
            record = {"schema_version": SCHEMA_VERSION, "sequence": sequence, **record}
            raw = _canonical_json(record) + b"\n"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("Could not append feedback transport evidence")
                    remaining = remaining[written:]
            finally:
                os.close(descriptor)
            return record

    def request(
        self,
        *,
        run_id: str,
        task_id: str,
        channel: str,
        operation: str,
        payload: Any,
        subject_id: str | None = None,
    ) -> str:
        exchange_id = uuid.uuid4().hex
        self._append(
            self._path(run_id, task_id),
            {
                "exchange_id": exchange_id,
                "record_kind": "request",
                "observed_at_unix": time.time(),
                "run_id": run_id,
                "task_id": task_id,
                "channel": channel,
                "operation": operation,
                "subject_id": subject_id,
                "payload_sha256": payload_sha256(payload),
            },
        )
        return exchange_id

    def response(
        self,
        *,
        exchange_id: str,
        run_id: str,
        task_id: str,
        channel: str,
        operation: str,
        payload: Any,
        status: str,
        subject_id: str | None = None,
    ) -> None:
        self._append(
            self._path(run_id, task_id),
            {
                "exchange_id": exchange_id,
                "record_kind": "response",
                "observed_at_unix": time.time(),
                "run_id": run_id,
                "task_id": task_id,
                "channel": channel,
                "operation": operation,
                "subject_id": subject_id,
                "status": status,
                "payload_sha256": payload_sha256(payload),
            },
        )


__all__ = [
    "JOURNAL_FILE",
    "SCHEMA_VERSION",
    "FeedbackTransportRecorder",
    "payload_sha256",
]
