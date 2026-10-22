"""Container host pins and isolated network endpoints must describe the same route."""
from types import SimpleNamespace
import logging

import pytest

from sforge.harness import network_isolation as network
from sforge.harness import run_agent as runner
from sforge.harness.config import SForgeConfig


@pytest.mark.parametrize("backend_name", ["docker", "k8s", "e2b"])
def test_startup_reuses_host_pin_when_dns_changes(tmp_path, monkeypatch, backend_name):
    config = SForgeConfig(log_dir=tmp_path, agent_api_base_url="https://api.example.test/v1")
    agent = SimpleNamespace(
        timeout=30, _config=config, default_model="fixture", model_env=None,
        default_api_base_url=None, augment_env=lambda env, model: None, install_cmds=[],
    )
    task = SimpleNamespace(
        task_id="fixture", cwd="/workspace", submit_paths=["answer.txt"], submit_exclude=[],
        game_mode=False, judge=SimpleNamespace(eval_timeout=1), work_image_key="work",
        judge_image_key="judge", work=SimpleNamespace(cpu_limit=1, mem_limit="1g"),
    )
    observed = {}
    resolutions = []
    handle = object()

    def resolve(host, logger):
        resolutions.append(host)
        return ["203.0.113.10", "203.0.113.11"] if len(resolutions) == 1 else ["198.51.100.20"]

    def create(*args, **kwargs):
        observed["hosts"] = kwargs["extra_hosts"].copy()
        return handle

    def isolate(container, endpoints, logger):
        observed["endpoints"] = endpoints

        def apply():
            # Stop after the real startup path constructed its network policy.
            raise KeyboardInterrupt

        return SimpleNamespace(apply=apply, cleanup=lambda: None)

    backend = SimpleNamespace(
        backend_name=backend_name, image_exists=lambda key: True,
        remove_container_by_name=lambda name: None, create_container=create,
        start_container=lambda value: None, get_container_gateway_ip=lambda value: "192.0.2.1",
        create_network_isolation=isolate, cleanup_container=lambda *args: None,
    )
    monkeypatch.setattr(network, "resolve_hostname", resolve)
    monkeypatch.setattr(network, "check_iptables_permission", lambda: True)
    monkeypatch.setattr(network, "cleanup_stale_chains", lambda logger: None)
    monkeypatch.setattr(runner, "_build_agent_env", lambda *args: {})
    monkeypatch.setattr(runner, "_e2b_install_env", lambda env, agent: env)
    monkeypatch.setattr(runner, "_install_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "get_admin_secret", lambda path: "fixture-secret")
    monkeypatch.setattr(runner, "_extract_archive_from_container", lambda *args: b"fixture")
    monkeypatch.setattr(runner.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"token": "fixture-token"},
    ))
    monkeypatch.setattr(runner.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"entries": []},
    ))

    runner.run_agent(
        task, agent, config, backend, "run", internet=False,
        judge_url="http://host.docker.internal:9000", disable_auto_eval=True,
        disable_stop_hook=True,
    )
    assert observed["endpoints"][0] == network.AllowedEndpoint(ip="192.0.2.1", port=9000)
    api = observed["endpoints"][1:]
    if backend_name == "e2b":
        assert resolutions == []
        assert "api.example.test" not in observed["hosts"]
        assert api == [network.AllowedEndpoint(ip="", port=443, hostname="api.example.test")]
    else:
        assert api == [network.AllowedEndpoint(
            ip=observed["hosts"]["api.example.test"], port=443, hostname="api.example.test",
        )]
        assert resolutions == ["api.example.test"]


def test_pinned_judge_and_api_do_not_resolve_again(monkeypatch):
    monkeypatch.setattr(network, "resolve_hostname", lambda *args: pytest.fail("unexpected DNS lookup"))
    endpoints = network.build_allowed_endpoints(
        "https://judge.example.test:8443", "https://api.example.test:9443/v1", "192.0.2.1",
        logging.getLogger(__name__),
        host_ip_overrides={"judge.example.test": "203.0.113.10", "api.example.test": "203.0.113.11"},
    )
    assert endpoints == [
        network.AllowedEndpoint(ip="203.0.113.10", port=8443, hostname="judge.example.test"),
        network.AllowedEndpoint(ip="203.0.113.11", port=9443, hostname="api.example.test"),
    ]


@pytest.mark.parametrize("value", ["host-gateway", "not-an-address", "::1"])
def test_invalid_local_ipv4_pin_fails_closed(value):
    with pytest.raises(ValueError):
        network.build_allowed_endpoints(
            "http://host.docker.internal:9000", "https://api.example.test", "192.0.2.1",
            logging.getLogger(__name__), host_ip_overrides={"api.example.test": value},
        )


def test_remote_hostname_rules_ignore_local_pins():
    endpoints = network.build_allowed_endpoints(
        "https://judge.example.test", "https://api.example.test", "", logging.getLogger(__name__),
        resolve_hostnames=False, host_ip_overrides={"api.example.test": "203.0.113.10"},
    )
    assert endpoints == [
        network.AllowedEndpoint(ip="", port=443, hostname="judge.example.test"),
        network.AllowedEndpoint(ip="", port=443, hostname="api.example.test"),
    ]


def test_gateway_and_ip_literals_keep_their_original_routes(monkeypatch):
    monkeypatch.setattr(network, "resolve_hostname", lambda *args: pytest.fail("unexpected DNS lookup"))
    endpoints = network.build_allowed_endpoints(
        "http://host.docker.internal:9000", "http://192.0.2.10:8080", "192.0.2.1",
        logging.getLogger(__name__), host_ip_overrides={"host.docker.internal": "host-gateway"},
    )
    assert endpoints == [
        network.AllowedEndpoint(ip="192.0.2.1", port=9000),
        network.AllowedEndpoint(ip="192.0.2.10", port=8080),
    ]


def test_unpinned_names_keep_dns_fallback(monkeypatch):
    monkeypatch.setattr(network, "resolve_hostname", lambda *args: ["203.0.113.10", "203.0.113.11"])
    endpoints = network.build_allowed_endpoints(
        "http://host.docker.internal:9000", "https://api.example.test", "192.0.2.1",
        logging.getLogger(__name__),
    )
    assert endpoints[1:] == [
        network.AllowedEndpoint(ip=ip, port=443, hostname="api.example.test")
        for ip in ["203.0.113.10", "203.0.113.11"]
    ]


def test_real_docker_pin_connectivity_and_port_isolation(monkeypatch):
    """Opt-in: use only a cached image and temporary host TCP listeners."""
    from contextlib import ExitStack
    import os
    import socket
    import uuid

    import docker

    image = os.environ.get("SFORGE_DOCKER_TEST_IMAGE")
    if not image or os.environ.get("SFORGE_NETWORK_TESTS") != "1":
        pytest.skip("Set SFORGE_NETWORK_TESTS=1 and SFORGE_DOCKER_TEST_IMAGE (python3)")
    client = docker.from_env()
    container = None
    isolation = None
    try:
        client.images.get(image)  # An opted-in test must fail on a missing cache/daemon.
        gateway = client.networks.get("bridge").attrs["IPAM"]["Config"][0]["Gateway"]
        pins = {"api.example.test": gateway, "host.docker.internal": gateway}
        container = client.containers.create(
            image, command=["sleep", "120"], detach=True,
            name=f"sforge-network-test-{uuid.uuid4().hex}",
            extra_hosts=pins, cap_drop=["NET_RAW"], mem_limit="128m",
        )
        container.start()
        monkeypatch.setattr(network, "resolve_hostname", lambda *args: ["203.0.113.10"])
        with ExitStack() as stack:
            ports = []
            for _ in range(3):
                listener = stack.enter_context(socket.socket())
                listener.bind((gateway, 0))
                listener.listen()
                ports.append(listener.getsockname()[1])
            judge_port, api_port, denied_port = ports
            endpoints = network.build_allowed_endpoints(
                f"http://host.docker.internal:{judge_port}",
                f"http://api.example.test:{api_port}", gateway, logging.getLogger(__name__),
                host_ip_overrides=pins,
            )
            isolation = network.NetworkIsolation(container, endpoints, logging.getLogger(__name__))
            isolation.apply()
            probe = """
import socket, sys
try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1):
        pass
except OSError:
    sys.exit(1)
"""
            for host, port in [("api.example.test", api_port), ("host.docker.internal", judge_port)]:
                assert container.exec_run(["python3", "-c", probe, host, str(port)]).exit_code == 0
            assert container.exec_run([
                "python3", "-c", probe, "api.example.test", str(denied_port),
            ]).exit_code == 1
    finally:
        try:
            if isolation is not None:
                isolation.cleanup()
        finally:
            try:
                if container is not None:
                    container.remove(force=True)
            finally:
                client.close()
