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

import importlib
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from sforge.harness.agent.codex import CodexAgent
from sforge.harness.api_proxy import APIProxy
from sforge.harness.config import SForgeConfig

runner = importlib.import_module("sforge.harness.run_agent")
_HOST_TOKEN = "-".join(("synthetic", "host", "token"))  # noqa: FLY002
_CONTAINER_TOKEN = "-".join(("synthetic", "container", "token"))  # noqa: FLY002


def _direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def test_codex_environment_scrubs_every_provider_credential_alias():
    provider_secret = _HOST_TOKEN
    config = SForgeConfig(
        agent_api_key=provider_secret,
        agent_extra_env={
            "OPENAI_API_KEY": provider_secret,
            "CODEX_API_KEY": provider_secret,
        },
    )
    env = runner._build_agent_env(
        CodexAgent(config),
        api_key_override="sforge-host-credential",
        api_base_url_override="http://host.docker.internal:43123",
    )

    assert env["OPENAI_API_KEY"] == "sforge-host-credential"
    assert "CODEX_API_KEY" not in env
    assert env["OPENAI_BASE_URL"] == "http://host.docker.internal:43123"
    assert provider_secret not in repr(env)


def test_host_headers_replace_container_credentials():
    observed = {}

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            observed.update({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": self.rfile.read(length),
            })
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"accepted")

        def log_message(self, format, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = APIProxy(
        target_url=f"http://127.0.0.1:{upstream.server_port}/provider/v1",
        host="127.0.0.1",
        injected_headers={"Authorization": f"Bearer {_HOST_TOKEN}"},
    )
    proxy._session.trust_env = False
    proxy.start()
    try:
        with _direct_session() as client:
            response = client.post(
                proxy.local_url + "/responses",
                headers={"Authorization": f"Bearer {_CONTAINER_TOKEN}"},
                data=b"request-body",
                timeout=5,
            )
        assert response.status_code == 200
        assert response.content == b"accepted"
        assert observed == {
            "path": "/provider/v1/responses",
            "authorization": f"Bearer {_HOST_TOKEN}",
            "body": b"request-body",
        }
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)


def test_proxy_streams_codex_response_events_before_response_completion(caplog):
    first_upstream_event = threading.Event()
    release_upstream = threading.Event()
    first_downstream_event = threading.Event()
    consumer_finished = threading.Event()
    consumer_errors: list[Exception] = []

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            assert self.path == "/provider/v1/responses?source=codex"
            assert b'"stream": true' in self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            first_upstream_event.set()
            release_upstream.wait(timeout=5)
            self.wfile.write(b"data: second\n\n")
            self.wfile.flush()

        def log_message(self, format, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = APIProxy(
        target_url=f"http://127.0.0.1:{upstream.server_port}/provider/v1",
        host="127.0.0.1",
    )
    proxy._session.trust_env = False
    caplog.set_level(logging.INFO)
    proxy.start()

    def consume_first_event():
        try:
            with (
                _direct_session() as client,
                client.post(
                    proxy.local_url + "/responses?source=codex",
                    data=b'{"model": "synthetic-model", "input": "private-prompt", "stream": true}',
                    stream=True,
                    timeout=5,
                ) as response,
            ):
                response.raise_for_status()
                received = b""
                for chunk in response.iter_content(chunk_size=1):
                    received += chunk
                    if b"\n\n" in received:
                        first_downstream_event.set()
                        break
        except requests.RequestException as exc:
            consumer_errors.append(exc)
        finally:
            consumer_finished.set()

    consumer = threading.Thread(target=consume_first_event, daemon=True)
    consumer.start()
    try:
        assert first_upstream_event.wait(timeout=2)
        assert first_downstream_event.wait(timeout=1)
    finally:
        release_upstream.set()
        consumer_finished.wait(timeout=5)
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)
    assert not consumer_errors
    assert "method=POST path=/responses body_size=71" in caplog.text
    assert "status=200 content_type=text/event-stream" in caplog.text
    assert "chunk_count=2 terminal_reason=upstream_eof" in caplog.text
    assert "private-prompt" not in caplog.text
    assert "source=codex" not in caplog.text


def test_proxy_telemetry_records_upstream_error_without_content(caplog):
    provider_secret = _HOST_TOKEN
    request_secret = "private-request-content"
    proxy = APIProxy(
        target_url="https://provider.invalid/v1",
        host="127.0.0.1",
        injected_headers={"Authorization": f"Bearer {provider_secret}"},
    )
    proxy._session.trust_env = False

    def fail_request(*args, **kwargs):
        raise RuntimeError(f"request carried {provider_secret} and {request_secret}")

    proxy._session.request = fail_request
    caplog.set_level(logging.INFO)
    proxy.start()
    try:
        with _direct_session() as client:
            response = client.post(
                proxy.local_url + "/responses?opaque=private-query-value",
                data=request_secret,
                timeout=5,
            )
        assert response.status_code == 502
    finally:
        proxy.stop()

    assert "method=POST path=/responses body_size=23" in caplog.text
    assert "status=None content_type=None ttfb_ms=None chunk_count=0" in caplog.text
    assert "terminal_reason=upstream_error" in caplog.text
    assert provider_secret not in caplog.text
    assert request_secret not in caplog.text
    assert "private-query-value" not in caplog.text


def test_proxy_telemetry_records_client_disconnect(caplog):
    upstream_stopped = threading.Event()

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            block = b"data: event\n\n" + b"x" * (256 * 1024)
            try:
                while True:
                    self.wfile.write(block)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                upstream_stopped.set()

        def log_message(self, format, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = APIProxy(
        target_url=f"http://127.0.0.1:{upstream.server_port}/provider/v1",
        host="127.0.0.1",
    )
    proxy._session.trust_env = False
    caplog.set_level(logging.INFO)
    proxy.start()
    try:
        client = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        client.sendall(
            b"POST /responses HTTP/1.0\r\n"
            b"Content-Length: 2\r\n"
            b"Content-Type: application/json\r\n\r\n{}"
        )
        assert b"200 OK" in client.recv(4096)
        client.close()
        deadline = time.monotonic() + 5
        while "terminal_reason=client_disconnect" not in caplog.text:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)

    assert upstream_stopped.wait(timeout=5)
    assert "method=POST path=/responses body_size=2" in caplog.text
    assert "status=200 content_type=text/event-stream" in caplog.text
    assert "terminal_reason=client_disconnect" in caplog.text


def test_proxy_generated_error_does_not_reflect_host_secret(caplog):
    provider_secret = _HOST_TOKEN
    proxy = APIProxy(
        target_url="https://provider.invalid/v1",
        host="127.0.0.1",
        injected_headers={"Authorization": f"Bearer {provider_secret}"},
    )
    proxy._session.trust_env = False

    def fail_request(*args, **kwargs):
        raise RuntimeError(f"request carried {provider_secret}")

    proxy._session.request = fail_request
    caplog.set_level(logging.WARNING)
    proxy.start()
    try:
        with _direct_session() as client:
            response = client.get(proxy.local_url + "/responses", timeout=5)
        assert response.status_code == 502
        assert provider_secret not in response.text
        assert provider_secret not in caplog.text
        assert "RuntimeError" in caplog.text
    finally:
        proxy.stop()


def test_proxy_rejects_clients_outside_run_container():
    proxy = APIProxy(
        target_url="https://provider.invalid/v1",
        host="127.0.0.1",
        injected_headers={"Authorization": f"Bearer {_HOST_TOKEN}"},
    )
    proxy.restrict_clients({"192.0.2.10"})
    proxy.start()
    try:
        with _direct_session() as client:
            response = client.get(proxy.local_url + "/responses", timeout=5)
        assert response.status_code == 403
    finally:
        proxy.stop()


def test_unstarted_proxy_can_be_closed_after_setup_failure():
    proxy = APIProxy(
        target_url="https://provider.invalid/v1",
        host="127.0.0.1",
        injected_headers={"Authorization": f"Bearer {_HOST_TOKEN}"},
    )
    proxy.stop()
