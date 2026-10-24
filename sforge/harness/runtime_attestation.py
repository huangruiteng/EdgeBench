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

"""Host-signed, append-only evidence for one solver runtime.

The signing key is read by the host runner and is never forwarded to a work or
judge container.  Each observation is chained to the previous signed payload,
so a later analysis process can neither insert a missing runtime phase nor
rewrite an observed fact without invalidating verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "sforge_runtime_attestation_v1"
JOURNAL_FILE = "runtime-attestation.jsonl"
LATEST_FILE = "runtime-attestation.json"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _read_signing_key(path: Path) -> bytes:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Runtime attestation key must be a regular file")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(
            "Runtime attestation key must not be accessible by group or others"
        )
    key = path.read_bytes()
    if len(key) < 32:
        raise ValueError("Runtime attestation key must contain at least 32 bytes")
    return key


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RuntimeAttestationRecorder:
    """Persist signed runtime observations under a task's log directory."""

    def __init__(
        self,
        log_dir: Path,
        *,
        key_file: Path,
        runner_source_revision: str,
        run_id: str,
        task_id: str,
        protocol: dict[str, Any],
        resume: bool = False,
    ) -> None:
        if not runner_source_revision.strip():
            raise ValueError(
                "SFORGE_RUNNER_SOURCE_REVISION is required when runtime "
                "attestation is enabled"
            )
        self._log_dir = log_dir
        self._key = _read_signing_key(key_file)
        self._key_id = "sha256:" + hashlib.sha256(self._key).hexdigest()[:24]
        self._identity = {
            "run_id": run_id,
            "task_id": task_id,
            "runner_source_revision": runner_source_revision,
            "protocol_sha256": hashlib.sha256(_canonical_json(protocol)).hexdigest(),
        }
        self._protocol = protocol
        self._sequence = 0
        self._previous_payload_sha256: str | None = None

        journal = self._log_dir / JOURNAL_FILE
        latest = self._log_dir / LATEST_FILE
        if journal.exists() or latest.exists():
            if not resume or not journal.is_file():
                raise FileExistsError(
                    "Runtime attestation artifacts already exist; a new runner may "
                    "not append under the same run/task identity"
                )
            records = verify_runtime_attestation(
                journal, key_file,
                expected_run_id=run_id, expected_task_id=task_id,
                expected_source_revision=runner_source_revision,
            )
            first_identity = records[0]["payload"]["identity"]
            if first_identity.get("protocol_sha256") != self._identity["protocol_sha256"]:
                raise ValueError("Restored runtime protocol does not match attested protocol")
            self._sequence = len(records)
            self._previous_payload_sha256 = records[-1]["signature"]["payload_sha256"]

    @property
    def identity(self) -> dict[str, str]:
        return dict(self._identity)

    def record(
        self,
        phase: str,
        status: str,
        facts: dict[str, Any],
        *,
        evidence_files: dict[str, Path] | None = None,
    ) -> dict[str, Any]:
        if not phase or not status:
            raise ValueError("Runtime attestation phase and status are required")
        evidence: dict[str, str] = {}
        for name, path in sorted((evidence_files or {}).items()):
            if path.is_file():
                evidence[name] = "sha256:" + _sha256_file(path)

        self._sequence += 1
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "sequence": self._sequence,
            "previous_payload_sha256": self._previous_payload_sha256,
            "observed_at_unix": time.time(),
            "phase": phase,
            "status": status,
            "identity": self._identity,
            "protocol": self._protocol if self._sequence == 1 else None,
            "facts": facts,
            "evidence": evidence,
        }
        canonical = _canonical_json(payload)
        payload_sha256 = hashlib.sha256(canonical).hexdigest()
        record = {
            "payload": payload,
            "signature": {
                "algorithm": "hmac-sha256",
                "key_id": self._key_id,
                "payload_sha256": payload_sha256,
                "value": hmac.new(self._key, canonical, hashlib.sha256).hexdigest(),
            },
        }
        raw = _canonical_json(record) + b"\n"
        journal_path = self._log_dir / JOURNAL_FILE
        descriptor = os.open(
            journal_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            remaining = memoryview(raw)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("Could not append runtime attestation record")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        latest_path = self._log_dir / LATEST_FILE
        temporary = latest_path.with_suffix(latest_path.suffix + ".tmp")
        temporary.write_bytes(json.dumps(record, indent=2, ensure_ascii=False).encode())
        os.chmod(temporary, 0o600)
        temporary.replace(latest_path)
        self._previous_payload_sha256 = payload_sha256
        return record


def verify_runtime_attestation(
    journal_path: Path,
    key_file: Path,
    *,
    expected_run_id: str | None = None,
    expected_task_id: str | None = None,
    expected_source_revision: str | None = None,
) -> list[dict[str, Any]]:
    """Verify every record, its ordering and its identity binding."""
    key = _read_signing_key(key_file)
    expected_key_id = "sha256:" + hashlib.sha256(key).hexdigest()[:24]
    records: list[dict[str, Any]] = []
    previous: str | None = None
    identity: dict[str, Any] | None = None
    with journal_path.open("rb") as stream:
        for line_number, raw in enumerate(stream, 1):
            if not raw.strip():
                raise ValueError(f"Empty attestation record at line {line_number}")
            record = json.loads(raw)
            payload = record.get("payload")
            signature = record.get("signature")
            if not isinstance(payload, dict) or not isinstance(signature, dict):
                raise ValueError(f"Invalid attestation record at line {line_number}")
            canonical = _canonical_json(payload)
            digest = hashlib.sha256(canonical).hexdigest()
            expected_signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
            if signature.get("algorithm") != "hmac-sha256":
                raise ValueError(f"Unknown attestation algorithm at line {line_number}")
            if signature.get("key_id") != expected_key_id:
                raise ValueError(f"Attestation key mismatch at line {line_number}")
            if signature.get("payload_sha256") != digest:
                raise ValueError(f"Attestation payload digest mismatch at line {line_number}")
            if not hmac.compare_digest(str(signature.get("value", "")), expected_signature):
                raise ValueError(f"Attestation signature mismatch at line {line_number}")
            if payload.get("sequence") != line_number:
                raise ValueError(f"Attestation sequence mismatch at line {line_number}")
            if payload.get("previous_payload_sha256") != previous:
                raise ValueError(f"Attestation chain mismatch at line {line_number}")
            current_identity = payload.get("identity")
            if identity is None:
                identity = current_identity
            if current_identity != identity:
                raise ValueError(f"Attestation identity changed at line {line_number}")
            previous = digest
            records.append(record)

    if not records or identity is None:
        raise ValueError("Runtime attestation journal is empty")
    for field, expected in (
        ("run_id", expected_run_id),
        ("task_id", expected_task_id),
        ("runner_source_revision", expected_source_revision),
    ):
        if expected is not None and identity.get(field) != expected:
            raise ValueError(f"Attestation {field} does not match the expected value")
    return records
