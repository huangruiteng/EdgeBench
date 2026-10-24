import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from sforge.harness.runtime_attestation import RuntimeAttestationRecorder
from sforge.harness.runtime_qualification import (
    POLICY_SCHEMA_VERSION,
    qualify_runtime_attestation,
)


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _fixture(tmp_path: Path):
    key = tmp_path / "attestation.key"
    key.write_bytes(b"test-only-runtime-attestation-key-32-bytes")
    os.chmod(key, 0o600)
    root = tmp_path / "run"
    root.mkdir()
    run_id = "run-1"
    task_id = "case-1"
    source = "0123456789abcdef"
    protocol = {
        "backend": "docker",
        "solver_timeout_seconds": 64800,
        "background_evaluation": {"enabled": True, "interval_seconds": 300},
        "active_evaluation": {"cooldown_seconds": 3600},
    }
    recorder = RuntimeAttestationRecorder(
        root,
        key_file=key,
        runner_source_revision=source,
        run_id=run_id,
        task_id=task_id,
        protocol=protocol,
    )
    recorder.record("admission", "accepted", {"restored_execution": False})
    ready = {
        "container": {"backend": "docker", "name": "container"},
        "network_boundary": {
            "restricted_requested": True,
            "restricted_applied": True,
        },
        "credential_boundary": {
            "mode": "host_gateway",
            "real_credential_absent_from_container": True,
            "gateway_client_ip_restricted": True,
        },
    }
    recorder.record("solver_ready", "ready", ready)

    solver = {
        "schema_version": 1,
        "status": "timed_out",
        "segments": [{
            "index": 1,
            "started_at": 100.0,
            "finished_at": 200.0,
            "elapsed_seconds": 100.0,
            "timed_out": True,
        }],
    }
    history = {
        "run_id": run_id,
        "best_score": -0.75,
        "best_pass_rate": 0.5,
        "agent_submissions": 2,
        "auto_submissions": 3,
        "entries": [],
    }
    _write(root / "solver_execution.json", solver)
    _write(root / "run_history.json", history)
    history_sha = hashlib.sha256((root / "run_history.json").read_bytes()).hexdigest()
    grading = {
        "status": "complete",
        "reason": "drained",
        "pending_submissions": 0,
        "elapsed_seconds": 12.5,
        "history_observed": True,
        "history_sha256": history_sha,
    }
    worker = {
        "profile": "native-goal",
        "model": "fixture-model",
        "stop_hook": False,
        "feedback": "native",
    }
    _write(root / "grading_status.json", grading)
    _write(root / "worker-profile.json", worker)
    recorder.record(
        "terminal",
        "complete",
        {
            "solver": {
                "timed_out": True,
                "runtime_seconds": 100.0,
                "execution_status": "timed_out",
            },
            "evaluation": grading,
            "outcome": {
                "grading_status": "complete",
                "grading_reason": "drained",
                "agent_submissions": 2,
                "background_submissions": 3,
                "best_score": -0.75,
                "best_pass_rate": 0.5,
            },
        },
        evidence_files={
            "solver_execution.json": root / "solver_execution.json",
            "grading_status.json": root / "grading_status.json",
            "run_history.json": root / "run_history.json",
            "worker-profile.json": root / "worker-profile.json",
        },
    )
    policy = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_id": "edgebench-native-hourly-v1",
        "expected_protocol": protocol,
        "expected_solver_ready": ready,
        "expected_worker_profile": worker,
        "require_complete_grading": True,
    }
    return key, root, policy, run_id, task_id, source


def _qualify(fixture):
    key, root, policy, run_id, task_id, source = fixture
    return qualify_runtime_attestation(
        root / "runtime-attestation.jsonl",
        key,
        evidence_root=root,
        policy=policy,
        expected_run_id=run_id,
        expected_task_id=task_id,
        expected_source_revision=source,
    )


def test_qualifies_complete_signed_runtime_without_analyst_backfill(tmp_path):
    receipt = _qualify(_fixture(tmp_path))

    assert receipt["runtime_qualified"] is True
    assert receipt["blockers"] == []
    assert all(receipt["checks"].values())
    assert receipt["timing"] == {
        "solver_started_at_unix": 100.0,
        "solver_finished_at_unix": 200.0,
        "solver_runtime_seconds": 100.0,
        "evaluation_drain_seconds": 12.5,
    }
    assert receipt["public_boundary"]["analyst_backfill_used"] is False


def test_policy_mismatch_fails_closed(tmp_path):
    fixture = _fixture(tmp_path)
    fixture[2]["expected_worker_profile"]["stop_hook"] = True

    receipt = _qualify(fixture)

    assert receipt["runtime_qualified"] is False
    assert "worker_profile_stop_hook_mismatch" in receipt["blockers"]


def test_changed_signed_evidence_fails_closed(tmp_path):
    fixture = _fixture(tmp_path)
    root = fixture[1]
    worker = json.loads((root / "worker-profile.json").read_text())
    worker["profile"] = "official"
    _write(root / "worker-profile.json", worker)

    receipt = _qualify(fixture)

    assert receipt["runtime_qualified"] is False
    assert "signed_evidence_worker_profile_digest_mismatch" in receipt["blockers"]


def test_cli_emits_public_receipt_and_requires_qualification(tmp_path):
    key, root, policy, run_id, task_id, source = _fixture(tmp_path)
    policy_path = tmp_path / "policy.json"
    _write(policy_path, policy)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sforge",
            "qualify-attestation",
            "--journal",
            str(root / "runtime-attestation.jsonl"),
            "--evidence-root",
            str(root),
            "--policy",
            str(policy_path),
            "--key-file",
            str(key),
            "--expected-run-id",
            run_id,
            "--expected-task-id",
            task_id,
            "--expected-source-revision",
            source,
            "--require-qualified",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    receipt = json.loads(completed.stdout)
    assert receipt["runtime_qualified"] is True
    assert str(tmp_path) not in completed.stdout
