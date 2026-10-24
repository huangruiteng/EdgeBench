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

"""Reduce a signed SForge journal to a fail-closed runtime qualification.

The policy is preregistered by the experiment owner.  It may require exact
subsets of the signed protocol, the observed solver boundary and the immutable
worker profile.  Qualification derives every claim from the HMAC journal and
the evidence files whose digests occur in its terminal record; an analyst
cannot fill in missing booleans after a run.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sforge.harness.runtime_attestation import verify_runtime_attestation

POLICY_SCHEMA_VERSION = "sforge_runtime_qualification_policy_v1"
RECEIPT_SCHEMA_VERSION = "sforge_runtime_qualification_v1"
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_EVIDENCE_FILES = {
    "solver_execution": "solver_execution.json",
    "grading_status": "grading_status.json",
    "run_history": "run_history.json",
    "game_history": "game_history.json",
    "worker_profile": "worker-profile.json",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
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
        raise TypeError("Runtime qualification evidence must be a JSON object")
    return value


def _public_id(value: object, field: str) -> str:
    text = str(value or "")
    if not _PUBLIC_ID.fullmatch(text):
        raise ValueError(f"Runtime qualification {field} is not a public identifier")
    return text


def _subset_failures(
    actual: object,
    expected: object,
    *,
    prefix: str,
) -> list[str]:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [f"{prefix}_type_mismatch"]
        failures: list[str] = []
        for key, value in expected.items():
            label = re.sub(r"[^A-Za-z0-9_]+", "_", str(key)).strip("_") or "field"
            if key not in actual:
                failures.append(f"{prefix}_{label}_missing")
                continue
            failures.extend(
                _subset_failures(actual[key], value, prefix=f"{prefix}_{label}")
            )
        return failures
    if isinstance(expected, list):
        if not isinstance(actual, list) or actual != expected:
            return [f"{prefix}_mismatch"]
        return []
    return [] if actual == expected else [f"{prefix}_mismatch"]


def _finite_nonnegative(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _same_number(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is right
    if (
        not isinstance(left, (int, float))
        or isinstance(left, bool)
        or not math.isfinite(float(left))
        or not isinstance(right, (int, float))
        or isinstance(right, bool)
        or not math.isfinite(float(right))
    ):
        return False
    return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)


def _load_signed_evidence(
    *,
    evidence_root: Path,
    signed_digests: Mapping[str, object],
    alias: str,
    failures: list[str],
) -> dict[str, Any] | None:
    expected_name = _EVIDENCE_FILES[alias]
    signed = signed_digests.get(expected_name)
    if not isinstance(signed, str) or not signed.startswith("sha256:"):
        failures.append(f"signed_evidence_{alias}_missing")
        return None
    root = evidence_root.resolve()
    path = evidence_root / expected_name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
        failures.append(f"signed_evidence_{alias}_unavailable")
        return None
    if signed != "sha256:" + _sha256_file(path):
        failures.append(f"signed_evidence_{alias}_digest_mismatch")
        return None
    try:
        return _read_json_object(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        failures.append(f"signed_evidence_{alias}_invalid")
        return None


def qualify_runtime_attestation(
    journal_path: Path,
    key_file: Path,
    *,
    evidence_root: Path,
    policy: Mapping[str, Any],
    expected_run_id: str,
    expected_task_id: str,
    expected_source_revision: str,
) -> dict[str, Any]:
    """Verify and reduce one terminal run without analyst-authored facts."""

    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("Runtime qualification policy schema is not supported")
    policy_id = _public_id(policy.get("policy_id"), "policy_id")
    if not isinstance(policy.get("expected_protocol", {}), Mapping):
        raise TypeError("Runtime qualification expected_protocol must be an object")
    if not isinstance(policy.get("expected_solver_ready", {}), Mapping):
        raise TypeError("Runtime qualification expected_solver_ready must be an object")
    if not isinstance(policy.get("expected_worker_profile", {}), Mapping):
        raise TypeError("Runtime qualification expected_worker_profile must be an object")

    records = verify_runtime_attestation(
        journal_path,
        key_file,
        expected_run_id=expected_run_id,
        expected_task_id=expected_task_id,
        expected_source_revision=expected_source_revision,
    )
    failures: list[str] = []
    phases = [str(record["payload"].get("phase") or "") for record in records]
    if phases != ["admission", "solver_ready", "terminal"]:
        failures.append("runtime_phase_sequence_mismatch")

    first = records[0]["payload"]
    terminal = records[-1]["payload"]
    ready_records = [
        record["payload"]
        for record in records
        if record["payload"].get("phase") == "solver_ready"
    ]
    ready = ready_records[0] if len(ready_records) == 1 else {}
    if first.get("status") != "accepted":
        failures.append("runtime_admission_not_accepted")
    if ready.get("status") != "ready":
        failures.append("runtime_solver_not_ready")
    if terminal.get("phase") != "terminal" or terminal.get("status") != "complete":
        failures.append("runtime_terminal_not_complete")
    failures.extend(
        _subset_failures(
            first.get("protocol"), policy.get("expected_protocol", {}),
            prefix="protocol",
        )
    )
    failures.extend(
        _subset_failures(
            ready.get("facts"), policy.get("expected_solver_ready", {}),
            prefix="solver_ready",
        )
    )

    signed_digests = terminal.get("evidence")
    if not isinstance(signed_digests, Mapping):
        failures.append("terminal_signed_evidence_missing")
        signed_digests = {}
    evidence = {
        alias: _load_signed_evidence(
            evidence_root=evidence_root,
            signed_digests=signed_digests,
            alias=alias,
            failures=failures,
        )
        for alias in ("solver_execution", "grading_status", "worker_profile")
    }
    history_aliases = [
        alias for alias in ("run_history", "game_history")
        if isinstance(signed_digests.get(_EVIDENCE_FILES[alias]), str)
    ]
    if len(history_aliases) != 1:
        failures.append("terminal_history_evidence_ambiguous")
        history = None
        history_alias = None
    else:
        history_alias = history_aliases[0]
        history = _load_signed_evidence(
            evidence_root=evidence_root,
            signed_digests=signed_digests,
            alias=history_alias,
            failures=failures,
        )

    worker_profile = evidence["worker_profile"] or {}
    failures.extend(
        _subset_failures(
            worker_profile,
            policy.get("expected_worker_profile", {}),
            prefix="worker_profile",
        )
    )

    facts = terminal.get("facts") if isinstance(terminal.get("facts"), Mapping) else {}
    solver_facts = facts.get("solver") if isinstance(facts.get("solver"), Mapping) else {}
    evaluation_facts = (
        facts.get("evaluation")
        if isinstance(facts.get("evaluation"), Mapping)
        else {}
    )
    outcome = facts.get("outcome") if isinstance(facts.get("outcome"), Mapping) else {}
    solver = evidence["solver_execution"] or {}
    grading = evidence["grading_status"] or {}

    if solver.get("status") != solver_facts.get("execution_status"):
        failures.append("solver_execution_status_mismatch")
    segments = solver.get("segments")
    if not isinstance(segments, list) or not segments:
        failures.append("solver_execution_segments_missing")
        segments = []
    previous_finished: float | None = None
    for segment in segments:
        if not isinstance(segment, Mapping):
            failures.append("solver_execution_segment_invalid")
            continue
        started, finished = segment.get("started_at"), segment.get("finished_at")
        elapsed = segment.get("elapsed_seconds")
        if not all(_finite_nonnegative(value) for value in (started, finished, elapsed)):
            failures.append("solver_execution_timing_invalid")
            continue
        if float(finished) < float(started) or (
            previous_finished is not None and float(started) < previous_finished
        ):
            failures.append("solver_execution_timing_order_invalid")
        previous_finished = float(finished)
    if not _finite_nonnegative(solver_facts.get("runtime_seconds")):
        failures.append("solver_runtime_seconds_invalid")

    for field in (
        "status", "reason", "pending_submissions", "elapsed_seconds",
        "history_observed", "history_sha256",
    ):
        if grading.get(field) != evaluation_facts.get(field):
            failures.append(f"terminal_evaluation_{field}_mismatch")
    if policy.get("require_complete_grading", True):
        if grading.get("status") != "complete":
            failures.append("grading_not_complete")
        if grading.get("pending_submissions") != 0:
            failures.append("grading_pending_submissions")
        if grading.get("history_observed") is not True:
            failures.append("grading_history_not_observed")
        if not _finite_nonnegative(grading.get("elapsed_seconds")):
            failures.append("grading_elapsed_seconds_invalid")

    if history is not None and history_alias is not None:
        history_path = evidence_root / _EVIDENCE_FILES[history_alias]
        if grading.get("history_sha256") != _sha256_file(history_path):
            failures.append("grading_history_digest_mismatch")
        if history.get("run_id") != expected_run_id:
            failures.append("history_run_id_mismatch")
        for field in ("agent_submissions", "auto_submissions"):
            outcome_field = (
                field if field != "auto_submissions" else "background_submissions"
            )
            if history.get(field) != outcome.get(outcome_field):
                failures.append(f"history_{field}_mismatch")
        if not _same_number(history.get("best_score"), outcome.get("best_score")):
            failures.append("history_best_score_mismatch")
        if not _same_number(history.get("best_pass_rate"), outcome.get("best_pass_rate")):
            failures.append("history_best_pass_rate_mismatch")

    failures = list(dict.fromkeys(failures))
    identity = first["identity"]
    policy_sha256 = hashlib.sha256(_canonical_json(dict(policy))).hexdigest()
    solver_started = min(
        (float(item["started_at"]) for item in segments
         if isinstance(item, Mapping) and _finite_nonnegative(item.get("started_at"))),
        default=None,
    )
    solver_finished = max(
        (float(item["finished_at"]) for item in segments
         if isinstance(item, Mapping) and _finite_nonnegative(item.get("finished_at"))),
        default=None,
    )
    return {
        "ok": True,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "runtime_qualified": not failures,
        "classification": (
            "runtime_integrity_qualified" if not failures
            else "runtime_integrity_not_qualified"
        ),
        "policy_id": policy_id,
        "policy_sha256": policy_sha256,
        "run_id": _public_id(identity.get("run_id"), "run_id"),
        "task_id": _public_id(identity.get("task_id"), "task_id"),
        "runner_source_revision": _public_id(
            identity.get("runner_source_revision"), "runner_source_revision"
        ),
        "protocol_sha256": identity.get("protocol_sha256"),
        "record_count": len(records),
        "phases": phases,
        "checks": {
            "signature_chain_verified": True,
            "terminal_complete": "runtime_terminal_not_complete" not in failures,
            "protocol_matches_policy": not any(
                item.startswith("protocol_") for item in failures
            ),
            "solver_boundary_matches_policy": not any(
                item.startswith("solver_ready_") for item in failures
            ),
            "worker_profile_matches_policy": not any(
                item.startswith("worker_profile_") for item in failures
            ),
            "signed_evidence_complete": not any(
                item.startswith(("signed_evidence_", "terminal_history_evidence_"))
                for item in failures
            ),
            "evaluation_drained": not any(
                item.startswith(("grading_", "terminal_evaluation_"))
                for item in failures
            ),
            "timing_complete": not any(
                "timing" in item or "seconds" in item
                for item in failures
            ),
        },
        "timing": {
            "solver_started_at_unix": solver_started,
            "solver_finished_at_unix": solver_finished,
            "solver_runtime_seconds": solver_facts.get("runtime_seconds"),
            "evaluation_drain_seconds": grading.get("elapsed_seconds"),
        },
        "blockers": failures,
        "public_boundary": {
            "raw_evidence_included": False,
            "input_paths_included": False,
            "credential_values_included": False,
            "analyst_backfill_used": False,
        },
    }


__all__ = [
    "POLICY_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "qualify_runtime_attestation",
]
