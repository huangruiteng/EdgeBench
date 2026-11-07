import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from sforge.harness.benchmark_integrity_attestation import (
    write_benchmark_runtime_integrity_attestation,
)
from sforge.harness.runtime_attestation import RuntimeAttestationRecorder
from sforge.harness.runtime_qualification import (
    POLICY_SCHEMA_VERSION,
    qualify_runtime_attestation,
)


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _fixture(
    tmp_path: Path,
    *,
    with_active_feedback: bool = True,
    history_entries: list[dict] | None = None,
    feedback_records: list[dict] | None = None,
    solver_runtime_seconds: float = 0.01,
    max_agent_submissions: int | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    key = tmp_path / "attestation.key"
    key.write_bytes(b"test-only-runtime-attestation-key-32-bytes")
    os.chmod(key, 0o600)
    run_id = "run-1"
    task_id = "case-1"
    root = tmp_path / "runs" / run_id / task_id
    root.mkdir(parents=True)
    source = "0123456789abcdef"
    protocol = {
        "backend": "docker",
        "solver_timeout_seconds": 64800,
        "background_evaluation": {"enabled": True, "interval_seconds": 300},
        "active_evaluation": {
            "cooldown_seconds": 3600,
            "max_submissions": max_agent_submissions,
        },
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
    admitted_at = recorder.records[0]["payload"]["observed_at_unix"]
    ready = {
        "container": {"backend": "docker", "name": "container"},
        "network_boundary": {
            "restricted_requested": True,
            "restricted_applied": True,
        },
        "credential_boundary": {
            "mode": "host_gateway",
            "provider_credential_configured": True,
            "real_credential_absent_from_container": True,
            "gateway_client_ip_restricted": True,
        },
        "isolation_boundary": {
            "inspection_complete": True,
            "unprivileged": True,
            "host_pid_namespace_absent": True,
            "host_network_namespace_absent": True,
            "host_mounts_absent": True,
            "docker_socket_absent": True,
        },
        "evaluation_boundary": {
            "run_task_scope_enforced": True,
            "judge_token_absent_from_solver": True,
            "admin_secret_absent_from_solver": True,
            "evaluator_image_separate": True,
            "background_results_absent_from_solver_history": True,
        },
    }
    recorder.record("solver_ready", "ready", ready)

    solver_started = admitted_at + 0.01
    solver_finished = solver_started + solver_runtime_seconds

    solver = {
        "schema_version": 1,
        "status": "timed_out",
        "segments": [{
            "index": 1,
            "started_at": solver_started,
            "finished_at": solver_finished,
            "elapsed_seconds": 0.01,
            "timed_out": True,
        }],
    }
    if history_entries is None:
        history_entries = [{
            "type": "submission",
            "status": "completed",
            "submission_id": "submission-1",
            "task_id": task_id,
            "round": "agent-1",
            "submitted_at": solver_started + 0.001,
            "evaluation_started_at": solver_started + 0.002,
            "evaluation_completed_at": solver_started + 0.003,
            "archive_sha256": "a" * 64,
        }]
    history = {
        "run_id": run_id,
        "best_score": -0.75,
        "best_pass_rate": 0.5,
        "agent_submissions": sum(
            str(item.get("round") or "").startswith("agent-")
            for item in history_entries
        ),
        "auto_submissions": sum(
            str(item.get("round") or "").startswith("auto-")
            for item in history_entries
        ),
        "entries": history_entries,
    }
    _write(root / "solver_execution.json", solver)
    _write(root / "run_history.json", history)
    history_sha = hashlib.sha256((root / "run_history.json").read_bytes()).hexdigest()
    grading = {
        "status": "complete",
        "reason": "drained",
        "pending_submissions": 0,
        "elapsed_seconds": 12.5,
        "started_at_unix": solver_finished + 0.001,
        "completed_at_unix": solver_finished + 12.501,
        "history_observed": True,
        "history_sha256": history_sha,
        "history_file": "run_history.json",
    }
    worker = {
        "profile": "native-goal",
        "model": "fixture-model",
        "stop_hook": False,
        "feedback": "native",
    }
    _write(root / "grading_status.json", grading)
    _write(root / "worker-profile.json", worker)
    default_transport_records = [
        {
            "schema_version": "sforge_feedback_transport_v1",
            "sequence": 1,
            "exchange_id": "exchange-1",
            "record_kind": "request",
            "observed_at_unix": solver_started + 0.004,
            "run_id": run_id,
            "task_id": task_id,
            "channel": "active_submission",
            "operation": "result",
            "subject_id": "submission-1",
            "payload_sha256": "b" * 64,
        },
        {
            "schema_version": "sforge_feedback_transport_v1",
            "sequence": 2,
            "exchange_id": "exchange-1",
            "record_kind": "response",
            "observed_at_unix": solver_started + 0.005,
            "run_id": run_id,
            "task_id": task_id,
            "channel": "active_submission",
            "operation": "result",
            "subject_id": "submission-1",
            "subject_status": "completed",
            "status": "delivered",
            "payload_sha256": "c" * 64,
        },
    ]
    transport_records = (
        default_transport_records if feedback_records is None else feedback_records
    )
    if with_active_feedback:
        (root / "feedback-transport.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in transport_records),
            encoding="utf-8",
        )
    benchmark_integrity = write_benchmark_runtime_integrity_attestation(
        records=recorder.records,
        evidence_root=root,
    )
    evidence_files = {
        "solver_execution.json": root / "solver_execution.json",
        "grading_status.json": root / "grading_status.json",
        "run_history.json": root / "run_history.json",
        "worker-profile.json": root / "worker-profile.json",
        benchmark_integrity.name: benchmark_integrity,
    }
    if with_active_feedback:
        evidence_files["feedback-transport.jsonl"] = (
            root / "feedback-transport.jsonl"
        )
    recorder.record(
        "terminal",
        "complete",
        {
            "solver": {
                "timed_out": True,
                "runtime_seconds": 0.01,
                "execution_status": "timed_out",
            },
            "evaluation": grading,
            "outcome": {
                "grading_status": "complete",
                "grading_reason": "drained",
                "agent_submissions": history["agent_submissions"],
                "background_submissions": history["auto_submissions"],
                "best_score": -0.75,
                "best_pass_rate": 0.5,
            },
        },
        evidence_files=evidence_files,
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
        "solver_started_at_unix": receipt["timing"]["solver_started_at_unix"],
        "solver_finished_at_unix": receipt["timing"]["solver_finished_at_unix"],
        "solver_runtime_seconds": 0.01,
        "evaluation_drain_seconds": 12.5,
    }
    assert receipt["public_boundary"]["analyst_backfill_used"] is False


def test_qualifies_when_active_feedback_channel_was_unused(tmp_path):
    fixture = _fixture(tmp_path, with_active_feedback=False)

    receipt = _qualify(fixture)

    assert receipt["runtime_qualified"] is True
    assert receipt["benchmark_integrity_qualified"] is True
    assert receipt["benchmark_integrity_blockers"] == []
    integrity = json.loads(
        (fixture[1] / "benchmark-runtime-integrity-attestation.json").read_text()
    )
    assert integrity["feedback_transport"] == {
        "journal_present": False,
        "journal_valid": True,
        "request_count": 0,
        "response_ack_count": 0,
        "journal_sha256": None,
    }


def test_rejects_submission_history_that_violates_declared_cadence(tmp_path):
    base = time.time() + 0.1
    entries = []
    for index, (kind, offset) in enumerate(
        (("agent", 0), ("agent", 1), ("auto", 0), ("auto", 1)), 1
    ):
        entries.append({
            "type": "submission",
            "status": "completed",
            "submission_id": f"submission-{index}",
            "task_id": "case-1",
            "round": f"{kind}-{1 if index % 2 else 2}",
            "submitted_at": base + offset,
            "evaluation_started_at": base + offset + 0.1,
            "evaluation_completed_at": base + offset + 0.2,
            "archive_sha256": f"{index:x}" * 64,
        })

    receipt = _qualify(_fixture(tmp_path, history_entries=entries))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_submission_cadence_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_long_background_enabled_run_without_auto_submission(tmp_path):
    receipt = _qualify(_fixture(tmp_path, solver_runtime_seconds=1000))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_submission_cadence_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_background_cadence_that_stops_after_first_submission(tmp_path):
    now = time.time()
    entry = {
        "type": "submission",
        "status": "completed",
        "submission_id": "submission-1",
        "task_id": "case-1",
        "round": "auto-1",
        "submitted_at": now + 300,
        "evaluation_started_at": now + 300.1,
        "evaluation_completed_at": now + 301,
        "archive_sha256": "a" * 64,
    }
    receipt = _qualify(_fixture(
        tmp_path,
        history_entries=[entry],
        solver_runtime_seconds=1000,
    ))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_submission_cadence_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_qualifies_continuous_background_cadence_within_jitter(tmp_path):
    now = time.time()
    entries = [{
        "type": "submission",
        "status": "completed",
        "submission_id": f"submission-{index}",
        "task_id": "case-1",
        "round": f"auto-{index}",
        "submitted_at": now + offset,
        "evaluation_started_at": now + offset + 0.1,
        "evaluation_completed_at": now + offset + 1,
        "archive_sha256": f"{index:x}" * 64,
    } for index, offset in enumerate((300, 601, 902), 1)]

    receipt = _qualify(_fixture(
        tmp_path,
        with_active_feedback=False,
        history_entries=entries,
        solver_runtime_seconds=1000,
    ))

    assert receipt["benchmark_integrity_qualified"] is True
    assert receipt["benchmark_integrity_blockers"] == []


def test_rejects_round_numbers_that_disagree_with_submission_chronology(tmp_path):
    now = time.time()
    entries = [{
        "type": "submission",
        "status": "completed",
        "submission_id": f"submission-{index}",
        "task_id": "case-1",
        "round": f"auto-{index}",
        "submitted_at": now + offset,
        "evaluation_started_at": now + offset + 0.1,
        "evaluation_completed_at": now + offset + 1,
        "archive_sha256": f"{index:x}" * 64,
    } for index, offset in enumerate((600, 300, 900), 1)]

    receipt = _qualify(_fixture(
        tmp_path,
        history_entries=entries,
        solver_runtime_seconds=1000,
    ))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_submission_cadence_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_background_capture_before_previous_evaluation_completes(tmp_path):
    now = time.time()
    completion_offsets = (800, 801, 901)
    entries = [{
        "type": "submission",
        "status": "completed",
        "submission_id": f"submission-{index}",
        "task_id": "case-1",
        "round": f"auto-{index}",
        "submitted_at": now + offset,
        "evaluation_started_at": now + offset + 0.1,
        "evaluation_completed_at": now + completion_offsets[index - 1],
        "archive_sha256": f"{index:x}" * 64,
    } for index, offset in enumerate((300, 600, 900), 1)]

    receipt = _qualify(_fixture(
        tmp_path,
        history_entries=entries,
        solver_runtime_seconds=1000,
    ))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_submission_cadence_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_noncontiguous_submission_round_history(tmp_path):
    now = time.time() + 0.1
    entry = {
        "type": "submission",
        "status": "completed",
        "submission_id": "submission-2",
        "task_id": "case-1",
        "round": "agent-2",
        "submitted_at": now,
        "evaluation_started_at": now + 0.1,
        "evaluation_completed_at": now + 0.2,
        "archive_sha256": "a" * 64,
    }
    receipt = _qualify(_fixture(
        tmp_path,
        history_entries=[entry],
        max_agent_submissions=1,
    ))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_submission_cadence_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_evaluation_that_completed_after_drain(tmp_path):
    fixture = _fixture(tmp_path)
    root = fixture[1]
    grading = json.loads((root / "grading_status.json").read_text())
    drain_completed = grading["completed_at_unix"]

    # Rebuild a complete, signed run whose evaluator claims to finish after the
    # drain.  Passing this through the fixture avoids an unsigned-file mutation.
    entry = {
        "type": "submission",
        "status": "completed",
        "submission_id": "submission-1",
        "task_id": "case-1",
        "round": "agent-1",
        "submitted_at": drain_completed - 2,
        "evaluation_started_at": drain_completed - 1,
        "evaluation_completed_at": drain_completed + 100,
        "archive_sha256": "a" * 64,
    }
    receipt = _qualify(_fixture(tmp_path / "late", history_entries=[entry]))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_evaluation_after_drain" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_feedback_request_without_matching_response(tmp_path):
    request_only = [{
        "schema_version": "sforge_feedback_transport_v1",
        "sequence": 1,
        "exchange_id": "exchange-1",
        "record_kind": "request",
        "observed_at_unix": 2_000_000_000.0,
        "run_id": "run-1",
        "task_id": "case-1",
        "channel": "active_submission",
        "operation": "result",
        "subject_id": "submission-1",
        "payload_sha256": "b" * 64,
    }]

    receipt = _qualify(_fixture(tmp_path, feedback_records=request_only))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_feedback_journal_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_feedback_response_with_mismatched_request_identity(tmp_path):
    observed = time.time()
    records = [
        {
            "schema_version": "sforge_feedback_transport_v1",
            "sequence": 1,
            "exchange_id": "exchange-1",
            "record_kind": "request",
            "observed_at_unix": observed,
            "run_id": "run-1",
            "task_id": "case-1",
            "channel": "active_submission",
            "operation": "result",
            "subject_id": "submission-1",
            "payload_sha256": "b" * 64,
        },
        {
            "schema_version": "sforge_feedback_transport_v1",
            "sequence": 2,
            "exchange_id": "exchange-1",
            "record_kind": "response",
            "observed_at_unix": observed + 0.1,
            "run_id": "other-run",
            "task_id": "other-case",
            "channel": "game",
            "operation": "history",
            "subject_id": "other-submission",
            "status": "delivered",
            "subject_status": "completed",
            "payload_sha256": "c" * 64,
        },
    ]

    receipt = _qualify(_fixture(tmp_path, feedback_records=records))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_feedback_journal_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_active_result_request_without_submission_identity(tmp_path):
    observed = time.time()
    records = [
        {
            "schema_version": "sforge_feedback_transport_v1",
            "sequence": 1,
            "exchange_id": "exchange-1",
            "record_kind": "request",
            "observed_at_unix": observed,
            "run_id": "run-1",
            "task_id": "case-1",
            "channel": "active_submission",
            "operation": "result",
            "subject_id": None,
            "payload_sha256": "b" * 64,
        },
        {
            "schema_version": "sforge_feedback_transport_v1",
            "sequence": 2,
            "exchange_id": "exchange-1",
            "record_kind": "response",
            "observed_at_unix": observed + 0.1,
            "run_id": "run-1",
            "task_id": "case-1",
            "channel": "active_submission",
            "operation": "result",
            "subject_id": "submission-1",
            "subject_status": "completed",
            "status": "delivered",
            "payload_sha256": "c" * 64,
        },
    ]

    receipt = _qualify(_fixture(tmp_path, feedback_records=records))

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_feedback_journal_invalid" in (
        receipt["benchmark_integrity_blockers"]
    )


def test_rejects_active_feedback_bound_to_background_submission(tmp_path):
    now = time.time()
    entries = [{
        "type": "submission",
        "status": "completed",
        "submission_id": f"auto-submission-{index}",
        "task_id": "case-1",
        "round": f"auto-{index}",
        "submitted_at": now + offset,
        "evaluation_started_at": now + offset + 0.1,
        "evaluation_completed_at": now + offset + 1,
        "archive_sha256": f"{index:x}" * 64,
    } for index, offset in enumerate((300, 601, 902), 1)]
    feedback_records = [
        {
            "schema_version": "sforge_feedback_transport_v1",
            "sequence": 1,
            "exchange_id": "exchange-1",
            "record_kind": "request",
            "observed_at_unix": now + 302,
            "run_id": "run-1",
            "task_id": "case-1",
            "channel": "active_submission",
            "operation": "result",
            "subject_id": "auto-submission-1",
            "payload_sha256": "b" * 64,
        },
        {
            "schema_version": "sforge_feedback_transport_v1",
            "sequence": 2,
            "exchange_id": "exchange-1",
            "record_kind": "response",
            "observed_at_unix": now + 302.1,
            "run_id": "run-1",
            "task_id": "case-1",
            "channel": "active_submission",
            "operation": "result",
            "subject_id": "auto-submission-1",
            "subject_status": "completed",
            "status": "delivered",
            "payload_sha256": "c" * 64,
        },
    ]

    fixture = _fixture(
        tmp_path,
        history_entries=entries,
        feedback_records=feedback_records,
        solver_runtime_seconds=1000,
    )
    receipt = _qualify(fixture)

    assert receipt["benchmark_integrity_qualified"] is False
    assert "benchmark_integrity_feedback_delivery_history_unbound" in (
        receipt["benchmark_integrity_blockers"]
    )
    integrity = json.loads(
        (fixture[1] / "benchmark-runtime-integrity-attestation.json").read_text()
    )
    assert integrity["official_feedback_blinded"] is False


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
            "--require-benchmark-integrity",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    receipt = json.loads(completed.stdout)
    assert receipt["runtime_qualified"] is True
    assert str(tmp_path) not in completed.stdout
