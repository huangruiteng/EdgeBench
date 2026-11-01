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

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from unittest.mock import patch

from sforge.harness.judge_server import GameSessionState, JudgeState


class FakeBackend:
    backend_name = "docker"

    def cleanup_container(self, container, logger):
        return None


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def bare_state(tmp_path: Path) -> JudgeState:
    state = object.__new__(JudgeState)
    state.config = SimpleNamespace(log_dir=tmp_path)
    state.game_sessions = {}
    state.game_counters = {}
    state.run_history = {}
    state._game_lock = threading.Lock()
    state._history_lock = threading.Lock()
    state._get_backend = lambda run_id, task_id: FakeBackend()
    return state


def session(session_id: str, game_num: int = 1) -> GameSessionState:
    return GameSessionState(
        session_id=session_id,
        run_id="run-1",
        task_id="task-1",
        game_num=game_num,
        container=object(),
        container_url="http://game",
    )


def test_close_all_waits_for_inflight_step_before_archiving(tmp_path):
    state = bare_state(tmp_path)
    current = session("session-1")
    state.game_sessions[current.session_id] = current
    request_started = threading.Event()
    allow_response = threading.Event()

    def post(*args, **kwargs):
        request_started.set()
        assert allow_response.wait(5)
        return FakeResponse({
            "score": 13, "peak_score": 13, "max_score": 100,
            "moves": 98, "done": False, "observation": "after step",
        })

    with patch("sforge.harness.judge_server.requests.post", side_effect=post):
        step = threading.Thread(target=state.game_step, args=(current.session_id, "north"))
        step.start()
        assert request_started.wait(5)
        close = threading.Thread(
            target=state.close_all_game_sessions, args=("run-1", "task-1"),
        )
        close.start()
        time.sleep(0.05)
        assert close.is_alive()
        allow_response.set()
        step.join(5)
        close.join(5)

    assert not step.is_alive()
    assert not close.is_alive()
    archived = state.run_history["run-1/task-1"]
    assert len(archived) == 1
    assert archived[0]["moves"] == 98
    assert archived[0]["steps"][-1]["action"] == "north"
    stored = json.loads((
        tmp_path / "runs/run-1/task-1/submissions/game-1/game_result.json"
    ).read_text())
    assert stored == archived[0]


def test_game_number_commit_is_atomic(tmp_path):
    state = bare_state(tmp_path)
    barrier = threading.Barrier(24)

    def commit(index: int):
        current = session(f"session-{index}", game_num=0)
        barrier.wait()
        state._commit_game_session(current)

    workers = [threading.Thread(target=commit, args=(index,)) for index in range(24)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)

    assert sorted(item.game_num for item in state.game_sessions.values()) == list(range(1, 25))


def test_failed_cleanup_does_not_publish_sessions_with_held_operation_locks(tmp_path):
    state = bare_state(tmp_path)
    first, second = session("session-1"), session("session-2", game_num=2)
    state.game_sessions = {item.session_id: item for item in (first, second)}
    second_cleanup_started = threading.Event()
    reader_holds_registry = threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            self.lock.acquire()
            if threading.current_thread().name == "cleanup-status-reader":
                reader_holds_registry.set()
            return self

        def __exit__(self, *unused):
            self.lock.release()

    class FailingBackend:
        backend_name = "e2b"
        failing = True

        def cleanup_container(self, container, logger):
            if not self.failing:
                return
            if container is second.container:
                second_cleanup_started.set()
                assert reader_holds_registry.wait(2)
            raise RuntimeError("Synthetic cleanup failure")

    registry = ObservedLock()
    backend = FailingBackend()
    state._game_lock = registry
    state._get_backend = lambda run_id, task_id: backend
    errors, reads = [], []

    def close_all():
        try:
            state.close_all_game_sessions("run-1", "task-1")
        except Exception as error:
            errors.append(error)

    def read_status():
        try:
            reads.append(state.game_status(first.session_id))
        except KeyError:
            reads.append("temporarily unavailable")

    # Daemon threads keep the failing pre-fix case bounded. Events order the
    # status read between the two cleanup failures without a sleep race.
    closer = threading.Thread(target=close_all, daemon=True)
    reader = threading.Thread(target=read_status, name="cleanup-status-reader", daemon=True)
    with patch("sforge.harness.judge_server.requests.get", return_value=FakeResponse({"score": 9})):
        closer.start()
        assert second_cleanup_started.wait(2)
        reader.start()
        closer.join(2)
        reader.join(2)
        assert not closer.is_alive() and not reader.is_alive(), "Cleanup and status formed a lock cycle"
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert "session-1" in str(errors[0]) and "session-2" in str(errors[0])
        assert reads == ["temporarily unavailable"]
        assert set(state.game_sessions) == {first.session_id, second.session_id}
        assert not registry.lock.locked()
        assert all(not item.operation_lock.locked() for item in (first, second))
        assert state.game_status(first.session_id)["score"] == 9

    # Failed cleanup stays retryable and does not archive twice or lose a
    # registration. A later successful cleanup makes forward progress.
    backend.failing = False
    assert state.close_all_game_sessions("run-1", "task-1") == 2
    assert state.game_sessions == {}
    assert len(state.run_history["run-1/task-1"]) == 2
