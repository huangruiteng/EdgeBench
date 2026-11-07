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

"""Reduce live SForge observations to runner-owned benchmark integrity facts."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "benchmark_runtime_integrity_attestation_v0"
FILE_NAME = "benchmark-runtime-integrity-attestation.json"
REQUIRED_TRUE_FIELDS = (
    "agent_phase_isolated",
    "evaluator_sources_denied",
    "other_trials_denied",
    "controller_state_denied",
    "host_escape_denied",
    "external_shell_network_denied",
    "provider_credential_shell_excluded",
    "case_local_control_state",
    "canonical_control_state_root",
    "independent_verifier",
    "verifier_started_after_agent",
    "verifier_started_after_submission",
    "official_feedback_blinded",
    "evaluation_protocol_enforced",
    "evaluation_log_complete",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain a JSON object")
    return value


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _milliseconds(value: object) -> int | None:
    return round(float(value) * 1000) if _finite(value) else None


def _feedback_transport(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        return [], {
            "journal_present": False,
            # The recorder creates the journal lazily on the first active-feedback
            # request.  A missing file therefore proves an unused channel, not a
            # damaged transcript.  Keep presence separate so consumers can still
            # distinguish the two cases.
            "journal_valid": True,
            "request_count": 0,
            "response_ack_count": 0,
            "journal_sha256": None,
        }
    records: list[dict[str, Any]] = []
    valid = True
    try:
        for expected_sequence, raw in enumerate(path.read_text().splitlines(), 1):
            if not raw.strip():
                continue
            item = json.loads(raw)
            if not isinstance(item, dict) or item.get("sequence") != expected_sequence:
                valid = False
                continue
            records.append(item)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        valid = False

    requests = [item for item in records if item.get("record_kind") == "request"]
    responses = [item for item in records if item.get("record_kind") == "response"]
    request_ids = {str(item.get("exchange_id") or "") for item in requests}
    response_ids = {str(item.get("exchange_id") or "") for item in responses}
    valid = valid and bool(request_ids) and response_ids.issubset(request_ids)

    deliveries: list[dict[str, Any]] = []
    delivered_subjects: set[str] = set()
    for item in responses:
        subject = str(item.get("subject_id") or "")
        if (
            item.get("channel") != "active_submission"
            or item.get("operation") != "result"
            or item.get("status") != "delivered"
            or item.get("subject_status") not in {"completed", "error"}
            or not subject
            or subject in delivered_subjects
        ):
            continue
        delivered_at = _milliseconds(item.get("observed_at_unix"))
        payload_sha256 = str(item.get("payload_sha256") or "")
        if delivered_at is None or len(payload_sha256) != 64:
            valid = False
            continue
        delivered_subjects.add(subject)
        deliveries.append(
            {
                "channel_id": "active_submission_result",
                "submission_id": subject,
                "delivered_at_ms": delivered_at,
                "feedback_sha256": payload_sha256,
                "phase": "solving",
            }
        )
    return deliveries, {
        "journal_present": True,
        "journal_valid": valid,
        "request_count": len(requests),
        "response_ack_count": len(responses),
        "journal_sha256": _sha256_file(path),
    }


def build_benchmark_runtime_integrity_attestation(
    *,
    records: Sequence[Mapping[str, Any]],
    evidence_root: Path,
) -> dict[str, Any]:
    """Build a compact public-safe object only from runner-captured evidence."""

    payloads = [item.get("payload") for item in records]
    payloads = [item for item in payloads if isinstance(item, Mapping)]
    admission = next(
        (item for item in payloads if item.get("phase") == "admission"), {}
    )
    ready = next(
        (item for item in payloads if item.get("phase") == "solver_ready"), {}
    )
    identity = admission.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    protocol = admission.get("protocol")
    protocol = protocol if isinstance(protocol, Mapping) else {}
    ready_facts = ready.get("facts")
    ready_facts = ready_facts if isinstance(ready_facts, Mapping) else {}
    isolation = ready_facts.get("isolation_boundary")
    isolation = isolation if isinstance(isolation, Mapping) else {}
    network = ready_facts.get("network_boundary")
    network = network if isinstance(network, Mapping) else {}
    credential = ready_facts.get("credential_boundary")
    credential = credential if isinstance(credential, Mapping) else {}
    evaluation_boundary = ready_facts.get("evaluation_boundary")
    evaluation_boundary = (
        evaluation_boundary
        if isinstance(evaluation_boundary, Mapping)
        else {}
    )

    run_id = str(identity.get("run_id") or "")
    task_id = str(identity.get("task_id") or "")
    source_revision = str(identity.get("runner_source_revision") or "")
    canonical_root = evidence_root.resolve()
    canonical_control_state_root = (
        canonical_root.name == task_id
        and canonical_root.parent.name == run_id
        and not evidence_root.is_symlink()
    )

    solver = _read_json_object(evidence_root / "solver_execution.json")
    grading = _read_json_object(evidence_root / "grading_status.json")
    history_name = str(grading.get("history_file") or "")
    if history_name not in {"run_history.json", "game_history.json"}:
        raise ValueError("grading_status history_file is not a supported evidence file")
    history = _read_json_object(evidence_root / history_name)

    segments = solver.get("segments")
    segments = segments if isinstance(segments, list) else []
    solver_starts = [
        float(item["started_at"])
        for item in segments
        if isinstance(item, Mapping) and _finite(item.get("started_at"))
    ]
    solver_finishes = [
        float(item["finished_at"])
        for item in segments
        if isinstance(item, Mapping) and _finite(item.get("finished_at"))
    ]
    solver_started = min(solver_starts, default=None)
    solver_ended = max(solver_finishes, default=None)

    submissions: list[dict[str, Any]] = []
    timing_complete = True
    timing_ordered = True
    entries = history.get("entries")
    entries = entries if isinstance(entries, list) else []
    for item in entries:
        if not isinstance(item, Mapping) or item.get("type") != "submission":
            continue
        submitted = item.get("submitted_at")
        started = item.get("evaluation_started_at")
        completed = item.get("evaluation_completed_at")
        digest = str(item.get("archive_sha256") or "")
        complete = all(_finite(value) for value in (submitted, started, completed))
        timing_complete = timing_complete and complete and len(digest) == 64
        if complete:
            timing_ordered = timing_ordered and (
                float(submitted) <= float(started) <= float(completed)
            )
        submissions.append(
            {
                "submission_id": str(item.get("submission_id") or ""),
                "round_id": str(item.get("round") or ""),
                "status": str(item.get("status") or ""),
                "captured_at_ms": _milliseconds(submitted),
                "evaluation_started_at_ms": _milliseconds(started),
                "evaluation_completed_at_ms": _milliseconds(completed),
                "submission_sha256": digest or None,
            }
        )
    if entries and not submissions:
        timing_complete = False

    deliveries, transport = _feedback_transport(
        evidence_root / "feedback-transport.jsonl"
    )
    grading_complete = (
        grading.get("status") == "complete"
        and grading.get("pending_submissions") == 0
        and grading.get("history_observed") is True
        and _finite(grading.get("elapsed_seconds"))
    )
    history_digest_matches = (
        grading.get("history_sha256") == _sha256_file(evidence_root / history_name)
    )
    evaluator_after_agent = (
        bool(submissions)
        and solver_started is not None
        and all(
            item["evaluation_started_at_ms"] is not None
            and item["evaluation_started_at_ms"] >= round(solver_started * 1000)
            for item in submissions
        )
    )
    evaluator_after_submission = bool(submissions) and timing_ordered
    isolation_complete = all(
        isolation.get(field) is True
        for field in (
            "inspection_complete",
            "unprivileged",
            "host_pid_namespace_absent",
            "host_network_namespace_absent",
            "host_mounts_absent",
            "docker_socket_absent",
        )
    )
    evaluation_boundary_complete = all(
        evaluation_boundary.get(field) is True
        for field in (
            "run_task_scope_enforced",
            "judge_token_absent_from_solver",
            "admin_secret_absent_from_solver",
            "evaluator_image_separate",
            "background_results_absent_from_solver_history",
        )
    )
    protocol_enforced = (
        admission.get("status") == "accepted"
        and ready.get("status") == "ready"
        and isinstance(protocol.get("active_evaluation"), Mapping)
        and isinstance(protocol.get("background_evaluation"), Mapping)
        and timing_complete
        and timing_ordered
        and grading_complete
        and history_digest_matches
        and transport["journal_valid"]
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "authority": "runner",
        "benchmark_id": f"edgebench@{source_revision[:12]}",
        "case_id": task_id,
        "agent_phase_isolated": isolation_complete,
        "evaluator_sources_denied": (
            isolation_complete
            and evaluation_boundary.get("evaluator_image_separate") is True
        ),
        "other_trials_denied": (
            evaluation_boundary.get("run_task_scope_enforced") is True
        ),
        "controller_state_denied": isolation_complete,
        "host_escape_denied": isolation_complete,
        "external_shell_network_denied": (
            network.get("restricted_requested") is True
            and network.get("restricted_applied") is True
        ),
        "provider_credential_shell_excluded": all(
            credential.get(field) is True
            for field in (
                "provider_credential_configured",
                "real_credential_absent_from_container",
                "gateway_client_ip_restricted",
            )
        ) and credential.get("mode") == "host_gateway",
        "case_local_control_state": canonical_control_state_root,
        "canonical_control_state_root": canonical_control_state_root,
        "independent_verifier": (
            evaluation_boundary_complete and evaluator_after_agent
        ),
        "verifier_started_after_agent": evaluator_after_agent,
        "verifier_started_after_submission": evaluator_after_submission,
        "official_feedback_blinded": (
            evaluation_boundary.get(
                "background_results_absent_from_solver_history"
            )
            is True
        ),
        "evaluation_protocol_enforced": protocol_enforced,
        "evaluation_log_complete": (
            grading_complete and history_digest_matches and timing_complete
        ),
        "evaluation_protocol": {
            "run_id": run_id,
            "policy_sha256": identity.get("protocol_sha256"),
            "policy_admitted_at_ms": _milliseconds(
                admission.get("observed_at_unix")
            ),
            "solver_started_at_ms": _milliseconds(solver_started),
            "solver_ended_at_ms": _milliseconds(solver_ended),
            "evaluation_drain_started_at_ms": _milliseconds(
                grading.get("started_at_unix")
            ),
            "evaluation_drain_completed_at_ms": _milliseconds(
                grading.get("completed_at_unix")
            ),
            "evaluation_drain_seconds": grading.get("elapsed_seconds"),
            "submissions": submissions,
            "deliveries": deliveries,
            "evaluation_history_sha256": grading.get("history_sha256"),
        },
        "feedback_transport": transport,
        "public_boundary": {
            "raw_evaluation_content_recorded": False,
            "credential_values_recorded": False,
            "host_paths_recorded": False,
            "analyst_backfill_used": False,
        },
    }


def write_benchmark_runtime_integrity_attestation(
    *,
    records: Sequence[Mapping[str, Any]],
    evidence_root: Path,
) -> Path:
    """Atomically persist the attestation before its digest is host-signed."""

    value = build_benchmark_runtime_integrity_attestation(
        records=records,
        evidence_root=evidence_root,
    )
    path = evidence_root / FILE_NAME
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return path


def benchmark_runtime_integrity_failures(
    value: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_task_id: str,
    expected_source_revision: str,
) -> list[str]:
    """Validate the signed compact artifact without reading analyst facts."""

    failures: list[str] = []
    if value.get("schema_version") != SCHEMA_VERSION:
        failures.append("benchmark_integrity_schema_mismatch")
    if value.get("authority") != "runner":
        failures.append("benchmark_integrity_authority_not_runner")
    if value.get("benchmark_id") != f"edgebench@{expected_source_revision[:12]}":
        failures.append("benchmark_integrity_source_revision_mismatch")
    if value.get("case_id") != expected_task_id:
        failures.append("benchmark_integrity_task_mismatch")
    for field in REQUIRED_TRUE_FIELDS:
        if value.get(field) is not True:
            failures.append(f"benchmark_integrity_{field}_missing")

    protocol = value.get("evaluation_protocol")
    if not isinstance(protocol, Mapping):
        failures.append("benchmark_integrity_evaluation_protocol_missing")
        protocol = {}
    if protocol.get("run_id") != expected_run_id:
        failures.append("benchmark_integrity_run_mismatch")
    timestamps = [
        protocol.get(field)
        for field in (
            "policy_admitted_at_ms",
            "solver_started_at_ms",
            "solver_ended_at_ms",
            "evaluation_drain_started_at_ms",
            "evaluation_drain_completed_at_ms",
        )
    ]
    if not all(_finite(value) for value in timestamps):
        failures.append("benchmark_integrity_terminal_timing_incomplete")
    elif timestamps != sorted(timestamps):
        failures.append("benchmark_integrity_terminal_timing_order_invalid")

    submissions = protocol.get("submissions")
    if not isinstance(submissions, list) or not submissions:
        failures.append("benchmark_integrity_submission_timing_missing")
        submissions = []
    for item in submissions:
        if not isinstance(item, Mapping):
            failures.append("benchmark_integrity_submission_timing_invalid")
            continue
        timing = [
            item.get("captured_at_ms"),
            item.get("evaluation_started_at_ms"),
            item.get("evaluation_completed_at_ms"),
        ]
        if not all(_finite(timestamp) for timestamp in timing):
            failures.append("benchmark_integrity_submission_timing_incomplete")
        elif timing != sorted(timing):
            failures.append("benchmark_integrity_submission_timing_order_invalid")
        digest = item.get("submission_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            failures.append("benchmark_integrity_submission_digest_missing")

    transport = value.get("feedback_transport")
    if not isinstance(transport, Mapping):
        failures.append("benchmark_integrity_feedback_transport_missing")
    else:
        if transport.get("journal_valid") is not True:
            failures.append("benchmark_integrity_feedback_journal_invalid")
        for field in ("request_count", "response_ack_count"):
            if not _finite(transport.get(field)):
                failures.append(f"benchmark_integrity_feedback_{field}_invalid")
        if transport.get("journal_present") is not True and (
            transport.get("request_count") != 0
            or transport.get("response_ack_count") != 0
        ):
            failures.append("benchmark_integrity_feedback_journal_missing")
    public_boundary = value.get("public_boundary")
    if (
        not isinstance(public_boundary, Mapping)
        or public_boundary.get("analyst_backfill_used") is not False
    ):
        failures.append("benchmark_integrity_analyst_backfill_unknown")
    return list(dict.fromkeys(failures))


__all__ = [
    "FILE_NAME",
    "SCHEMA_VERSION",
    "benchmark_runtime_integrity_failures",
    "build_benchmark_runtime_integrity_attestation",
    "write_benchmark_runtime_integrity_attestation",
]
