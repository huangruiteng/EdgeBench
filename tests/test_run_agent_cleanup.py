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

"""Exercise run_agent cleanup without a container, model, or judge service."""

from __future__ import annotations

import hashlib
import importlib
import os
import signal
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import pytest

from sforge.harness.agent.base import Agent
from sforge.harness.backend.base import ContainerBackend, StreamingExecResult
from sforge.harness.config import SForgeConfig
from sforge.harness.task_spec import JudgeSpec, TaskSpec, WorkSpec
from sforge.harness.runtime_attestation import verify_runtime_attestation

runner = importlib.import_module("sforge.harness.run_agent")


class SyntheticAgent(Agent):
    name = "synthetic"
    install_cmds: ClassVar[list[str]] = ["install-synthetic-agent"]
    run_cmd = "run-synthetic-agent {prompt_file}"
    api_key_env = "SYNTHETIC_API_KEY"
    api_base_env = "SYNTHETIC_API_BASE"
    credential_env_names = ("SYNTHETIC_API_KEY", "SYNTHETIC_API_KEY_ALIAS")

    def credential_proxy_headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    config = SForgeConfig(log_dir=tmp_path)
    task = TaskSpec(
        task_id="cleanup-fixture", name="Cleanup fixture", base_image="python",
        platform="linux/amd64", cwd="/workspace", submit_paths=["."],
        submit_exclude=[],
        work=WorkSpec(specs_dir="", agent_query="Synthetic task", image_tag="fixture"),
        judge=JudgeSpec(eval_cmd="true", eval_timeout=1, parser="pytest", image_tag="fixture"),
    )
    backend = Mock(spec=ContainerBackend)
    backend.backend_name = "docker"
    backend.image_exists.return_value = True
    backend.exec_run.return_value = StreamingExecResult(output="", exit_code=0)
    handle = backend.create_container.return_value
    backend.exec_run_with_exit_code.return_value = StreamingExecResult(output="installed")
    backend.exec_run_with_timeout.side_effect = [
        StreamingExecResult(output="solver-output", elapsed_seconds=2),
        StreamingExecResult(output="{}"),
    ]

    history = {"best_score": 0.7, "best_pass_rate": 0.5, "best_round": "round-1",
               "agent_submissions": 1, "auto_submissions": 2, "entries": []}
    registration = Mock()
    registration.json.return_value = {
        "token": "synthetic-session", "feedback_relay_id": "synthetic-relay",
    }
    history_response = Mock()
    history_response.json.return_value = history
    monkeypatch.setattr(runner.requests, "post", Mock(return_value=registration))
    monkeypatch.setattr(runner.requests, "get", Mock(return_value=history_response))
    # Reject all unmocked HTTP paths rather than relying on unreachable URLs.
    monkeypatch.setattr(runner.requests.sessions.Session, "request",
                        Mock(side_effect=AssertionError("Unexpected network access")))
    monkeypatch.setattr(runner, "get_admin_secret", lambda _: "synthetic-admin")
    monkeypatch.setattr("sforge.harness.network_isolation.cleanup_stale_chains", Mock())
    archive = Mock(return_value=b"synthetic-archive")
    monkeypatch.setattr(runner, "_extract_archive_from_container", archive)
    closed = Mock(wraps=runner.close_logger)
    monkeypatch.setattr(runner, "close_logger", closed)
    previous_sigint = signal.getsignal(signal.SIGINT)

    def run(**kwargs):
        return runner.run_agent(
            task, SyntheticAgent(config), config, backend, "synthetic-run",
            timeout=5, disable_auto_resume=True, disable_stop_hook=True,
            **{"disable_auto_eval": True, **kwargs},
        )

    yield SimpleNamespace(backend=backend, handle=handle, run=run, close=closed,
                          config=config,
                          archive=archive, history=history, previous_sigint=previous_sigint)
    signal.signal(signal.SIGINT, previous_sigint)


def assert_cleaned(harness, handle):
    harness.backend.cleanup_container.assert_called_once()
    assert harness.backend.cleanup_container.call_args.args[0] is handle
    harness.close.assert_called_once()
    assert signal.getsignal(signal.SIGINT) == harness.previous_sigint


def test_docker_provider_credential_stays_in_host_gateway(harness, monkeypatch, tmp_path):
    provider_secret = "synthetic-provider-secret"
    proxies = []

    class FakeProxy:
        def __init__(self, **kwargs):
            self.port = 43123
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.allowed_ips = None
            proxies.append(self)

        def restrict_clients(self, allowed_ips):
            self.allowed_ips = set(allowed_ips)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr("sforge.harness.api_proxy.APIProxy", FakeProxy)
    harness.config.agent_api_key = provider_secret
    harness.config.agent_api_base_url = "https://provider.example/v1"
    harness.config.agent_extra_env.update({
        "SYNTHETIC_API_KEY": provider_secret,
        "SYNTHETIC_API_KEY_ALIAS": provider_secret,
    })
    harness.backend.get_container_ip.return_value = "192.0.2.7"

    result = harness.run()

    assert result.agent_output == "solver-output"
    assert len(proxies) == 1
    proxy = proxies[0]
    assert proxy.started and proxy.stopped
    assert proxy.allowed_ips == {"192.0.2.7"}
    assert proxy.kwargs["target_url"] == "https://provider.example/v1"
    assert proxy.kwargs["injected_headers"] == {
        "Authorization": f"Bearer {provider_secret}",
    }

    container_env = harness.backend.create_container.call_args.kwargs["environment"]
    assert container_env["SYNTHETIC_API_KEY"] == "sforge-host-credential"
    assert "SYNTHETIC_API_KEY_ALIAS" not in container_env
    assert container_env["NO_PROXY"].split(",") == ["host.docker.internal"]
    assert container_env["no_proxy"].split(",") == ["host.docker.internal"]
    assert provider_secret not in repr(harness.backend.mock_calls)
    for artifact in tmp_path.rglob("*"):
        if artifact.is_file():
            assert provider_secret not in artifact.read_text(errors="replace")


def test_runner_attests_observed_boundaries_and_terminal_evaluation(harness, tmp_path):
    key = tmp_path / "runtime-attestation.key"
    key.write_bytes(b"test-only-runtime-attestation-key-32-bytes")
    os.chmod(key, 0o600)
    harness.config.runtime_attestation_key_file = key
    harness.config.runner_source_revision = "test-source-revision"
    harness.handle.id = "container-id"
    harness.handle.name = "container-name"

    result = harness.run(submission_cooldown=1800)

    log_dir = tmp_path / "runs" / "synthetic-run" / "cleanup-fixture"
    records = verify_runtime_attestation(
        log_dir / "runtime-attestation.jsonl", key,
        expected_run_id="synthetic-run", expected_task_id="cleanup-fixture",
        expected_source_revision="test-source-revision",
    )
    assert [record["payload"]["phase"] for record in records] == [
        "admission", "solver_ready", "terminal",
    ]
    protocol = records[0]["payload"]["protocol"]
    assert protocol["active_evaluation"]["cooldown_seconds"] == 1800
    assert protocol["background_evaluation"] == {
        "enabled": False, "interval_seconds": 300,
    }
    boundaries = records[1]["payload"]["facts"]
    assert boundaries["container"]["backend"] == "docker"
    assert boundaries["container"]["name"] == "container-name"
    assert boundaries["container"]["id_sha256"] == hashlib.sha256(
        b"container-id"
    ).hexdigest()
    assert boundaries["container"]["work_image"].endswith("cleanup-fixture:fixture")
    assert boundaries["container"]["judge_image"].endswith("cleanup-fixture:fixture")
    assert boundaries["network_boundary"] == {
        "restricted_requested": False, "restricted_applied": False,
    }
    assert boundaries["credential_boundary"]["mode"] == "none"
    terminal = records[-1]["payload"]["facts"]
    assert terminal["evaluation"]["status"] == "complete"
    assert terminal["outcome"]["best_score"] == result.best_score


def test_attestation_configuration_fails_before_runtime_side_effects(harness, tmp_path):
    key = tmp_path / "runtime-attestation.key"
    key.write_bytes(b"test-only-runtime-attestation-key-32-bytes")
    os.chmod(key, 0o644)
    harness.config.runtime_attestation_key_file = key
    harness.config.runner_source_revision = "test-source-revision"

    with pytest.raises(PermissionError, match="group or others"):
        harness.run()

    harness.backend.image_exists.assert_not_called()
    harness.backend.create_container.assert_not_called()
    harness.close.assert_called_once()


@pytest.mark.parametrize("backend_name", ["docker", "k8s", "e2b"])
@pytest.mark.parametrize("timed_out", [False, True], ids=["nonzero-exit", "timeout"])
def test_install_failure_preserves_failure_and_cleans_up(harness, backend_name, timed_out):
    harness.backend.backend_name = backend_name
    harness.backend.exec_run_with_exit_code.return_value = StreamingExecResult(
        output="synthetic install failure", exit_code=23, timed_out=timed_out,
    )
    expected = "Install command timed out" if timed_out else "Install command failed (exit 23)"
    if backend_name == "e2b":
        with pytest.raises(RuntimeError, match=expected.replace("(", r"\(").replace(")", r"\)")) as error:
            harness.run()
        assert type(error.value) is RuntimeError
    else:
        result = harness.run()
        assert expected in result.agent_output
        assert result.best_score == harness.history["best_score"]
        assert result.resume_count == 0
    harness.backend.exec_run_with_timeout.assert_not_called()
    assert_cleaned(harness, harness.handle)


@pytest.mark.parametrize("backend_name", ["docker", "e2b"])
@pytest.mark.parametrize("stage", ["image", "create"])
def test_failure_before_handle_keeps_original_error(harness, backend_name, stage):
    harness.backend.backend_name = backend_name
    if stage == "image":
        harness.backend.image_exists.return_value = False
        expected = "not found"
    else:
        harness.backend.create_container.side_effect = RuntimeError("synthetic create failure")
        expected = "synthetic create failure"
    if backend_name == "e2b":
        with pytest.raises(RuntimeError, match=expected) as error:
            harness.run()
        assert type(error.value) is RuntimeError
    else:
        result = harness.run()
        assert expected in result.agent_output
    if stage == "image":
        harness.backend.create_container.assert_not_called()
    harness.backend.exec_run_with_timeout.assert_not_called()
    assert_cleaned(harness, None)


def test_interrupt_during_install_returns_interrupted_result(harness):
    harness.backend.exec_run_with_exit_code.side_effect = KeyboardInterrupt
    result = harness.run()
    assert result.agent_output == "Stopped by user (Ctrl+C)"
    assert result.resume_count == 0
    assert result.archive == b"synthetic-archive"
    harness.backend.exec_run_with_timeout.assert_not_called()
    assert_cleaned(harness, harness.handle)


@pytest.mark.parametrize("auto_eval", [False, True])
def test_success_preserves_result_and_stops_auto_eval(harness, monkeypatch, auto_eval):
    thread_factory = Mock()
    monkeypatch.setattr(runner.threading, "Thread", thread_factory)
    result = harness.run(disable_auto_eval=not auto_eval)
    assert result.agent_output == "solver-output"
    assert result.archive == b"synthetic-archive"
    assert result.best_score == 0.7
    assert result.total_rounds == 3
    assert result.runtime_seconds == 2
    assert not result.timed_out
    assert result.resume_count == 0
    if auto_eval:
        thread_factory.return_value.start.assert_called_once()
        stop_event = thread_factory.call_args.kwargs["args"][6]
        assert stop_event.is_set()
    else:
        thread_factory.assert_not_called()
    assert_cleaned(harness, harness.handle)
