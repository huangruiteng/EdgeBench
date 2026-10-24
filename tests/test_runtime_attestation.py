import json
import os
from types import SimpleNamespace

import pytest

from sforge.harness.runtime_attestation import (
    RuntimeAttestationRecorder,
    verify_runtime_attestation,
)
from sforge.cli import cmd_verify_attestation


def signing_key(tmp_path):
    path = tmp_path / "attestation.key"
    path.write_bytes(b"test-only-runtime-attestation-key-32-bytes")
    os.chmod(path, 0o600)
    return path


def test_signed_journal_binds_identity_protocol_and_evidence(tmp_path):
    key = signing_key(tmp_path)
    logs = tmp_path / "run"
    logs.mkdir()
    evidence = logs / "grading_status.json"
    evidence.write_text('{"status":"complete"}')
    recorder = RuntimeAttestationRecorder(
        logs,
        key_file=key,
        runner_source_revision="0123456789abcdef",
        run_id="run-1",
        task_id="case-1",
        protocol={"eval_interval": 300},
    )
    recorder.record("admission", "accepted", {"restored": False})
    recorder.record(
        "terminal", "complete", {"score": 1.0},
        evidence_files={"grading": evidence},
    )

    records = verify_runtime_attestation(
        logs / "runtime-attestation.jsonl", key,
        expected_run_id="run-1", expected_task_id="case-1",
        expected_source_revision="0123456789abcdef",
    )
    assert [item["payload"]["phase"] for item in records] == [
        "admission", "terminal",
    ]
    assert records[0]["payload"]["protocol"] == {"eval_interval": 300}
    assert records[1]["payload"]["evidence"]["grading"].startswith("sha256:")
    latest = json.loads((logs / "runtime-attestation.json").read_text())
    assert latest == records[-1]


def test_tampered_or_truncated_journal_does_not_verify(tmp_path):
    key = signing_key(tmp_path)
    logs = tmp_path / "run"
    logs.mkdir()
    recorder = RuntimeAttestationRecorder(
        logs, key_file=key, runner_source_revision="revision",
        run_id="run", task_id="case", protocol={},
    )
    recorder.record("admission", "accepted", {})
    recorder.record("terminal", "complete", {})
    journal = logs / "runtime-attestation.jsonl"
    lines = journal.read_text().splitlines()
    changed = json.loads(lines[1])
    changed["payload"]["status"] = "rewritten"
    journal.write_text(lines[0] + "\n" + json.dumps(changed) + "\n")

    with pytest.raises(ValueError, match="digest mismatch|signature mismatch"):
        verify_runtime_attestation(journal, key)


def test_key_permissions_and_run_identity_fail_closed(tmp_path):
    key = signing_key(tmp_path)
    os.chmod(key, 0o640)
    logs = tmp_path / "run"
    logs.mkdir()
    with pytest.raises(PermissionError, match="group or others"):
        RuntimeAttestationRecorder(
            logs, key_file=key, runner_source_revision="revision",
            run_id="run", task_id="case", protocol={},
        )

    os.chmod(key, 0o600)
    recorder = RuntimeAttestationRecorder(
        logs, key_file=key, runner_source_revision="revision",
        run_id="run", task_id="case", protocol={},
    )
    recorder.record("admission", "accepted", {})
    with pytest.raises(FileExistsError, match="already exist"):
        RuntimeAttestationRecorder(
            logs, key_file=key, runner_source_revision="revision",
            run_id="run", task_id="case", protocol={},
        )

    resumed = RuntimeAttestationRecorder(
        logs, key_file=key, runner_source_revision="revision",
        run_id="run", task_id="case", protocol={}, resume=True,
    )
    resumed.record("restored", "accepted", {})
    assert len(verify_runtime_attestation(logs / "runtime-attestation.jsonl", key)) == 2


def test_cli_verifies_expected_identity(tmp_path, capsys):
    key = signing_key(tmp_path)
    logs = tmp_path / "run"
    logs.mkdir()
    recorder = RuntimeAttestationRecorder(
        logs, key_file=key, runner_source_revision="revision",
        run_id="run", task_id="case", protocol={},
    )
    recorder.record("terminal", "complete", {})
    cmd_verify_attestation(SimpleNamespace(
        journal=str(logs / "runtime-attestation.jsonl"), key_file=str(key),
        expected_run_id="run", expected_task_id="case",
        expected_source_revision="revision",
    ))
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] is True
    assert result["phases"] == ["terminal"]
