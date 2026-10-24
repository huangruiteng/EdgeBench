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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from sforge.harness.api_proxy import APIProxy
from sforge.harness.agent.codex import CodexAgent
from sforge.harness.config import SForgeConfig


runner = importlib.import_module("sforge.harness.run_agent")
_HOST_TOKEN = "-".join(("synthetic", "host", "token"))
_CONTAINER_TOKEN = "-".join(("synthetic", "container", "token"))


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
