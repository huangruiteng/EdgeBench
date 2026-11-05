# Copyright (c) 2026 ByteDance Ltd. and/or its affiliates
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy at http://www.apache.org/licenses/LICENSE-2.0
"""Local dispatch admission, using synthetic responses and real client sockets."""

from contextlib import contextmanager
import socket
import threading
import time
from unittest.mock import Mock

import pytest
import requests

from sforge.harness import api_proxy


@contextmanager
def gateway(monkeypatch, *, deadline=None, shutdown=None, replies=None):
    clock = [100.0]
    # Replace only this module's clock, leaving HTTP/test timeouts real.
    monkeypatch.setattr(api_proxy, "time", Mock(monotonic=lambda: clock[0], time=time.time))
    proxy = api_proxy.APIProxy(
        "http://provider.invalid/v1", host="127.0.0.1",
        deadline_monotonic=deadline, shutdown_event=shutdown,
        rate_limit_retry_seconds=30,
    )
    calls = []
    responses = []

    def dispatch(**kwargs):
        calls.append((clock[0], kwargs))
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "text/plain"}
        response.raw.read1.side_effect = [b"accepted", b""]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(side_effect=lambda *args: response.close())
        if replies is not None:
            replies(response, len(calls), clock)
        responses.append(response)
        return response

    monkeypatch.setattr(proxy._session, "request", dispatch)
    proxy.start()
    client = requests.Session()
    client.trust_env = False
    try:
        yield proxy, client, clock, calls, responses
    finally:
        client.close()
        proxy.stop()


def test_late_fresh_request_is_rejected_without_upstream_admission(monkeypatch, caplog):
    caplog.set_level("INFO", logger=api_proxy.__name__)
    with gateway(monkeypatch, deadline=101) as (proxy, client, clock, calls, _):
        clock[0] = 101
        result = client.post(proxy.local_url, data=b"private-task", timeout=3)
        assert result.status_code == 503
        assert not calls
    assert "terminal_reason=deadline_exceeded" in caplog.text
    assert "private-task" not in caplog.text


@pytest.mark.parametrize("boundary", ["deadline", "stop", "shutdown"])
def test_headers_before_boundary_body_after_cannot_dispatch(monkeypatch, boundary):
    shutdown = threading.Event()
    with gateway(monkeypatch, deadline=101, shutdown=shutdown) as (proxy, _, clock, calls, _):
        body_read_started = threading.Event()
        handler = proxy._server.RequestHandlerClass
        original_setup = handler.setup

        def setup(self):
            original_setup(self)
            original_read = self.rfile.read

            def read(size):
                body_read_started.set()
                return original_read(size)

            self.rfile.read = read

        monkeypatch.setattr(handler, "setup", setup)
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=3) as connection:
            connection.sendall(b"POST /responses HTTP/1.0\r\nHost: local\r\nContent-Length: 4\r\n\r\n")
            assert body_read_started.wait(2)
            if boundary == "deadline":
                clock[0] = 101
            elif boundary == "stop":
                proxy.stop()  # Must return while the accepted handler waits.
            else:
                shutdown.set()
            connection.sendall(b"body")
            assert b" 503 " in connection.recv(4096)
        assert calls == []


def test_delayed_retry_wakeup_rechecks_absolute_deadline_and_closes_429(monkeypatch):
    def limited(response, number, clock):
        response.status_code = 429
        response.headers = {"Retry-After": "1"}

    with gateway(monkeypatch, deadline=110, replies=limited) as (proxy, client, clock, calls, responses):
        def late_wake(*args, **kwargs):
            clock[0] = 111
            return True
        monkeypatch.setattr(api_proxy, "_wait_for_retry", late_wake)
        assert client.post(proxy.local_url, timeout=3).status_code == 503
        assert [at for at, _ in calls] == [100]
        responses[0].close.assert_called_once()


def test_retry_after_beyond_inference_deadline_preserves_last_429(monkeypatch):
    def limited(response, number, clock):
        response.status_code = 429
        response.headers = {"Retry-After": "20"}
    with gateway(monkeypatch, deadline=110, replies=limited) as (proxy, client, _, calls, responses):
        result = client.post(proxy.local_url, timeout=3)
        assert result.status_code == 429
        assert result.headers["Retry-After"] == "20"
        assert len(calls) == 1
        responses[0].close.assert_called_once()


def test_admitted_429_arriving_after_deadline_is_preserved_without_retry(monkeypatch):
    def late_limit(response, number, clock):
        response.status_code = 429
        response.headers = {"Retry-After": "1"}
        clock[0] = 111
    with gateway(monkeypatch, deadline=110, replies=late_limit) as (proxy, client, _, calls, responses):
        assert client.post(proxy.local_url, timeout=3).status_code == 429
        assert len(calls) == 1
        responses[0].close.assert_called_once()


def test_admitted_response_may_finish_after_deadline_without_new_inference(monkeypatch, caplog):
    caplog.set_level("INFO", logger=api_proxy.__name__)
    def finish_late(response, number, clock):
        clock[0] = 120
    with gateway(monkeypatch, deadline=110, replies=finish_late) as (proxy, client, _, calls, responses):
        result = client.post(proxy.local_url, timeout=3)
        assert (result.status_code, result.content) == (200, b"accepted")
        assert client.post(proxy.local_url, timeout=3).status_code == 503
        assert len(calls) == 1 and calls[0][0] == 100
        responses[0].close.assert_called_once()
    assert "admitted_at_monotonic=100.000000" in caplog.text
    assert "deadline_monotonic=110" in caplog.text


def test_early_close_fences_admission_without_waiting_for_inflight(monkeypatch):
    with gateway(monkeypatch, deadline=110) as (proxy, client, _, calls, _):
        assert client.post(proxy.local_url, timeout=3).status_code == 200
        proxy.close_admission()
        assert client.post(proxy.local_url, timeout=3).status_code == 503
        assert len(calls) == 1


def test_stop_during_admitted_dispatch_does_not_deadlock_or_admit_a_second_call(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    results = []
    errors = []

    def blocked(response, number, clock):
        entered.set()
        assert release.wait(3)

    with gateway(monkeypatch, deadline=110, replies=blocked) as (proxy, client, _, calls, responses):
        def consume():
            try:
                with requests.Session() as first:
                    first.trust_env = False
                    results.append(first.post(proxy.local_url, timeout=5).status_code)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=consume)
        thread.start()
        try:
            assert entered.wait(2)
            proxy.close_admission()
            assert client.post(proxy.local_url, timeout=2).status_code == 503
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and not errors
        assert results == [200] and len(calls) == 1
        responses[0].close.assert_called_once()


@pytest.mark.parametrize("budgeted", [False, True])
def test_real_transport_redirect_cannot_bypass_budget_admission(budgeted):
    from test_api_proxy_retries import server

    replies = [(307, {"Location": "/redirected"}, b"redirect"), (200, {}, b"ok")]
    with server(replies) as (proxy, client, calls, _):
        if budgeted:
            proxy.set_deadline(time.monotonic() + 30)
        response = client.post(proxy.local_url, data=b"body", timeout=3, allow_redirects=False)
        assert response.status_code == (307 if budgeted else 200)
        assert len(calls) == (1 if budgeted else 2)


def test_real_transport_retries_keep_original_cutoff_and_provider_response():
    from test_api_proxy_retries import server

    with server([(429, {"Retry-After": "1"}, b"limited")], retry_seconds=30) as (
        proxy, client, calls, _,
    ):
        deadline = time.monotonic() + 1.5
        proxy.set_deadline(deadline)
        response = client.post(proxy.local_url, data=b"body", timeout=4)
        assert response.status_code == 429
        assert response.content == b"limited"
        assert len(calls) == 2
        time.sleep(max(0, deadline - time.monotonic()))
        assert client.post(proxy.local_url, timeout=3).status_code == 503
        assert len(calls) == 2


def test_deadline_can_be_bound_after_setup_but_never_extended(monkeypatch):
    with gateway(monkeypatch) as (proxy, client, clock, calls, _):
        proxy.set_deadline(110)
        proxy.set_deadline(120)
        clock[0] = 111
        assert client.post(proxy.local_url, timeout=3).status_code == 503
        assert not calls


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, "110"])
def test_invalid_deadline_rejected_before_binding_socket(value):
    with pytest.raises(ValueError, match="finite"):
        api_proxy.APIProxy("http://provider.invalid", deadline_monotonic=value)
