"""Non-finite optimization scores remain readable through feedback HTTP APIs."""

import math
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from sforge.harness import judge_server as judge
from sforge.harness.config import SForgeConfig
from sforge.harness.evolve_scripts import generate_submit_script


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "create_backend_from_config", lambda config: object())
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "BENCHMARK.yaml").write_text("name: nonfinite-feedback-test\n")
    state = judge.JudgeState(SForgeConfig(log_dir=tmp_path, tasks_dir=tasks))
    state.tokens["test-token"] = {
        "task_id": "example",
        "run_id": "sample",
        "feedback_relay_id": "test-relay",
        "next_agent": 2,
        "next_auto": 1,
        "max_agent_submissions": 3,
        "submission_cooldown": 0,
        "last_agent_submit_at": 0.0,
        "judge_cpu_limit": None,
        "judge_mem_limit": None,
    }
    report = {
        "score": float("inf"),
        "pass_rate": 0.0,
        "valid": False,
        "details": [
            {
                "name": "invalid-candidate",
                "status": "FAILED",
                "score": float("-inf"),
                "diagnostic": float("nan"),
            }
        ],
    }
    state.submissions["submission-1"] = {
        "status": judge.SubmissionStatus.COMPLETED,
        "task_id": "example",
        "run_id": "sample",
        "round": "agent-1",
        "report": report,
        "error": None,
        "archive_sha256": None,
        "archive_bytes": None,
    }
    state.run_history["sample/example"] = [
        {
            "type": "submission",
            "status": "completed",
            "submission_id": "submission-1",
            "task_id": "example",
            "round": "agent-1",
            "score": float("inf"),
            "pass_rate": 0.0,
            "valid": False,
        }
    ]
    monkeypatch.setattr(judge, "JudgeState", lambda config, **kwargs: state)
    app = judge.create_app(state.config)
    yield state, app
    state._reaper_stop.set()
    state._reaper_thread.join(timeout=2)


def assert_report_is_json_safe(report):
    assert report["score"] == "Infinity"
    assert report["details"][0]["score"] == "-Infinity"
    assert report["details"][0]["diagnostic"] == "NaN"


def test_result_routes_project_nonfinite_scores_without_mutating_state(service):
    state, app = service
    with TestClient(app, raise_server_exceptions=False) as client:
        host = client.get(
            "/api/v1/result/submission-1",
            params={"token": "test-token"},
        )
        assert host.status_code == 200, host.text
        assert_report_is_json_safe(host.json()["report"])

        relay = client.get("/api/v1/feedback/test-relay/result/submission-1")
        assert relay.status_code == 200, relay.text
        assert_report_is_json_safe(relay.json()["report"])

    raw = state.submissions["submission-1"]["report"]
    assert math.isinf(raw["score"]) and raw["score"] > 0
    assert math.isnan(raw["details"][0]["diagnostic"])


def test_history_routes_project_nonfinite_scores_and_deliver_feedback(service):
    state, app = service
    with TestClient(app, raise_server_exceptions=False) as client:
        host = client.get("/api/v1/history", params={"token": "test-token"})
        assert host.status_code == 200, host.text
        # Selection deliberately excludes non-finite candidates; the raw entry
        # remains explicit instead of making the whole history unreadable.
        assert host.json()["best_score"] is None
        assert host.json()["entries"][0]["score"] == "Infinity"

        relay = client.get("/api/v1/feedback/test-relay/history")
        assert relay.status_code == 200, relay.text
        assert relay.json()["best_score"] is None
        assert relay.json()["entries"][0]["score"] == "Infinity"

    assert math.isinf(state.run_history["sample/example"][0]["score"])


def test_generated_submit_cli_reads_nonfinite_result_and_history(service, tmp_path):
    for tool in ("bash", "curl", "jq"):
        if not shutil.which(tool):
            pytest.skip(f"CLI integration requires {tool}")
    _, app = service
    script = tmp_path / "sforge-submit"
    script.write_text(generate_submit_script())
    socket_handle = socket.socket()
    socket_handle.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [socket_handle]},
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        base = f"http://127.0.0.1:{socket_handle.getsockname()[1]}"
        env = {
            **os.environ,
            "SFORGE_FEEDBACK_URL": base + "/api/v1/feedback/test-relay",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        result = subprocess.run(
            ["bash", str(script), "--result", "submission-1"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Score:       Infinity" in result.stdout
        history = subprocess.run(
            ["bash", str(script), "--list"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert history.returncode == 0, history.stderr
        assert "Infinity" in history.stdout
    finally:
        server.should_exit = True
        thread.join(timeout=5)
