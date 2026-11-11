import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sforge.harness import judge_server
from sforge.harness.config import SForgeConfig
from sforge.harness.feedback_transport import (
    JOURNAL_FILE,
    FeedbackTransportRecorder,
    payload_sha256,
)


def test_feedback_registration_projects_stable_identity_without_host_token():
    state = object.__new__(judge_server.JudgeState)
    state._tokens_lock = threading.RLock()
    state.tokens = {
        "host-only-token": {
            "feedback_relay_id": "opaque-relay",
            "run_id": "run-1",
            "task_id": "case-1",
            "next_agent": 7,
            "last_agent_submit_at": 123.0,
        },
    }

    registration = state.resolve_feedback_registration("host-only-token")

    assert registration == {
        "feedback_relay_id": "opaque-relay",
        "run_id": "run-1",
        "task_id": "case-1",
    }
    assert "host-only-token" not in repr(registration)


def test_feedback_registration_rejects_an_ambiguous_relay():
    state = object.__new__(judge_server.JudgeState)
    state._tokens_lock = threading.RLock()
    state.tokens = {
        token: {
            "feedback_relay_id": "duplicate-relay",
            "run_id": "run-1",
            "task_id": task_id,
        }
        for token, task_id in (("token-1", "case-1"), ("token-2", "case-2"))
    }

    with pytest.raises(KeyError, match="Invalid feedback relay"):
        state.resolve_feedback_registration("token-1")


def test_feedback_transport_retains_digests_and_order_without_content(tmp_path):
    recorder = FeedbackTransportRecorder(tmp_path)
    request = {"action": "private solver action"}
    response = {"observation": "private evaluator output", "score": 7}
    exchange = recorder.request(
        run_id="run-1",
        task_id="case-1",
        channel="game",
        operation="step",
        payload=request,
        subject_id="session-1",
    )
    recorder.response(
        exchange_id=exchange,
        run_id="run-1",
        task_id="case-1",
        channel="game",
        operation="step",
        payload=response,
        status="completed",
        subject_id="session-1",
    )

    path = tmp_path / "runs/run-1/case-1" / JOURNAL_FILE
    raw = path.read_text()
    assert "private solver action" not in raw
    assert "private evaluator output" not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["sequence"] for record in records] == [1, 2]
    assert {record["exchange_id"] for record in records} == {exchange}
    assert records[0]["payload_sha256"] == payload_sha256(request)
    assert records[1]["payload_sha256"] == payload_sha256(response)
    assert records[0]["observed_at_unix"] <= records[1]["observed_at_unix"]


def test_active_feedback_records_typed_agent_only_provenance(tmp_path):
    recorder = FeedbackTransportRecorder(tmp_path)
    exchange = recorder.request(
        run_id="run-1", task_id="case-1", channel="active_submission",
        operation="history", payload={},
    )
    payload = {"entries": [{"round": "agent-1", "submission_id": "sub-1"}],
               "best_round": "agent-1", "agent_submissions": 1}
    recorder.response(
        exchange_id=exchange, run_id="run-1", task_id="case-1",
        channel="active_submission", operation="history", payload=payload,
        status="delivered",
    )
    records = [json.loads(line) for line in (
        tmp_path / "runs/run-1/case-1" / JOURNAL_FILE).read_text().splitlines()]
    assert records[-1]["provenance"] == {
        "policy": "active_feedback_projection_v1",
        "visibility_scope": "agent_submissions_only",
        "source_submission_ids": ["sub-1"],
    }


def test_active_feedback_rejects_background_history_before_delivery(tmp_path):
    recorder = FeedbackTransportRecorder(tmp_path)
    exchange = recorder.request(
        run_id="run-1", task_id="case-1", channel="active_submission",
        operation="history", payload={},
    )
    with pytest.raises(ValueError, match="non-agent evidence"):
        recorder.response(
            exchange_id=exchange, run_id="run-1", task_id="case-1",
            channel="active_submission", operation="history",
            payload={"entries": [{"round": "auto-1",
                                   "submission_id": "background-1"}]},
            status="delivered",
        )


def test_concurrent_feedback_records_keep_unique_monotonic_sequence(tmp_path):
    recorder = FeedbackTransportRecorder(tmp_path)

    def record(index):
        recorder.request(
            run_id="run-1",
            task_id="case-1",
            channel="active_submission",
            operation="history",
            payload={"index": index},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(40)))

    path = tmp_path / "runs/run-1/case-1" / JOURNAL_FILE
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["sequence"] for record in records] == list(range(1, 41))
    assert len({record["exchange_id"] for record in records}) == 40


@pytest.mark.parametrize("run_id,task_id", [("..", "case-1"), ("run-1", "..")])
def test_feedback_transport_rejects_unsafe_log_segments(tmp_path, run_id, task_id):
    recorder = FeedbackTransportRecorder(tmp_path)

    with pytest.raises(ValueError, match="safe log segments"):
        recorder.request(
            run_id=run_id,
            task_id=task_id,
            channel="game",
            operation="status",
            payload={},
        )


def test_game_routes_journal_each_exchange_without_action_or_observation(
    tmp_path, monkeypatch,
):
    states = []

    class TestState(judge_server.JudgeState):
        def load_tasks(self):
            self.tasks = {}
            states.append(self)

        def create_game_session(self, run_id, task_id):
            session = judge_server.GameSessionState(
                session_id="session-1",
                run_id=run_id,
                task_id=task_id,
                game_num=1,
                container=object(),
                container_url="http://game.invalid",
            )
            self.game_sessions[session.session_id] = session
            return session, {
                "observation": "private initial observation",
                "max_score": 10,
            }

        def game_step(self, session_id, action):
            assert action == "private solver action"
            return {
                "observation": "private next observation",
                "score": 3,
                "peak_score": 3,
                "max_score": 10,
                "moves": 1,
                "done": False,
            }

        def game_status(self, session_id):
            return {
                "score": 3,
                "peak_score": 3,
                "max_score": 10,
                "moves": 1,
                "done": False,
            }

        def close_game_session(self, session_id):
            self.game_sessions.pop(session_id)
            return {
                "final_score": 3,
                "peak_score": 3,
                "max_score": 10,
                "moves": 1,
            }

    monkeypatch.setattr(judge_server, "JudgeState", TestState)
    app = judge_server.create_app(
        SForgeConfig(log_dir=tmp_path),
        backend_factory=lambda config: SimpleNamespace(backend_name="k8s"),
    )

    with TestClient(app) as client:
        created = client.post("/api/v1/game/run-1/case-1/new", json={})
        assert created.status_code == 200
        wrong_route = client.get(
            "/api/v1/game/other-run/case-1/session-1/status",
        )
        assert wrong_route.status_code == 404
        stepped = client.post(
            "/api/v1/game/run-1/case-1/session-1/step",
            json={"action": "private solver action"},
        )
        assert stepped.status_code == 200
        status = client.get("/api/v1/game/run-1/case-1/session-1/status")
        assert status.status_code == 200
        closed = client.post("/api/v1/game/run-1/case-1/session-1/close")
        assert closed.status_code == 200

    path = tmp_path / "runs/run-1/case-1" / JOURNAL_FILE
    raw = path.read_text()
    assert "private solver action" not in raw
    assert "private initial observation" not in raw
    assert "private next observation" not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["operation"] for record in records] == [
        "new", "new", "step", "step", "status", "status", "close", "close",
    ]
    assert [record["record_kind"] for record in records] == [
        "request", "response", "request", "response",
        "request", "response", "request", "response",
    ]
    assert all(
        record.get("status") == "delivered"
        for record in records
        if record["record_kind"] == "response"
    )
    assert not (tmp_path / "runs/other-run").exists()
    assert states
