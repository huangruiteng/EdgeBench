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

"""Local reverse proxy for host-controlled provider API access.

When network isolation is enabled (internet=false) but the API endpoint
requires a corporate proxy, we cannot set proxy env vars inside the
container — iptables blocks all traffic except whitelisted IPs.

Solution: run a lightweight reverse proxy on the host that:
  1. Listens on <host>:<port>
  2. Forwards every request to the real API via the configured proxy
  3. The container reaches it via host.docker.internal:<port>

This avoids the container needing any proxy awareness or internet access.

Usage (separate terminal):
  python -m sforge proxy --target https://api.anthropic.com --port 9090
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests as req_lib

logger = logging.getLogger(__name__)
_STREAM_READ_SIZE = 64 * 1024


def _find_free_port(host: str = "0.0.0.0") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _make_handler(
    target_url: str,
    session: req_lib.Session,
    injected_headers: dict[str, str] | None = None,
    client_policy: dict[str, set[str] | None] | None = None,
):
    """Create a request handler class that proxies to target_url via session."""
    target_base = target_url.rstrip("/")
    host_headers = dict(injected_headers or {})
    replaced_header_names = {key.lower() for key in host_headers}
    policy = client_policy if client_policy is not None else {"allowed_ips": None}

    class ProxyHandler(BaseHTTPRequestHandler):
        # HTTP/1.0: connection close signals end-of-body, no need for
        # Content-Length or Transfer-Encoding: chunked.
        protocol_version = "HTTP/1.0"

        def do_request(self):
            allowed_ips = policy["allowed_ips"]
            if allowed_ips is not None and self.client_address[0] not in allowed_ips:
                self.send_error(403, "Client is not allowed")
                return
            upstream_url = f"{target_base}{self.path}"

            body = None
            content_length = self.headers.get("Content-Length")
            if content_length:
                body = self.rfile.read(int(content_length))

            fwd_headers = {}
            for key, val in self.headers.items():
                if key.lower() in (
                    "host",
                    "transfer-encoding",
                    "accept-encoding",
                    "proxy-authorization",
                ) or key.lower() in replaced_header_names:
                    continue
                fwd_headers[key] = val
            parsed = urlparse(target_base)
            fwd_headers["Host"] = parsed.hostname or parsed.netloc
            fwd_headers.update(host_headers)

            response_started = False
            try:
                with session.request(
                    method=self.command,
                    url=upstream_url,
                    headers=fwd_headers,
                    data=body,
                    timeout=300,
                    stream=True,
                ) as resp:
                    self.send_response(resp.status_code)
                    for key, val in resp.headers.items():
                        if key.lower() in (
                            "transfer-encoding",
                            "content-encoding",
                            "content-length",
                            "connection",
                            "keep-alive",
                        ):
                            continue
                        self.send_header(key, val)
                    self.end_headers()
                    response_started = True
                    # ``iter_content(chunk_size=None)`` asks urllib3 to read
                    # until EOF.  Provider SSE responses can remain open for
                    # minutes, so that mode withholds every event from the
                    # agent.  ``read1`` returns currently available decoded
                    # bytes without waiting to fill the requested buffer.
                    while chunk := resp.raw.read1(
                        _STREAM_READ_SIZE, decode_content=True
                    ):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # A solver timeout or cancellation can close a streaming model
                # response normally. The response socket is already unusable;
                # attempting to write a secondary 502 only creates another
                # exception and a noisy server traceback.
                logger.debug("api-proxy client disconnected during response")
            except Exception as exc:
                # Request exceptions can embed headers or request objects.  Do
                # not reflect their text to the untrusted container or logs.
                logger.warning("api-proxy upstream request failed: %s", type(exc).__name__)
                if not response_started:
                    try:
                        self.send_error(502, "Upstream request failed")
                    except (BrokenPipeError, ConnectionResetError):
                        logger.debug("api-proxy client disconnected before error response")

        do_GET = do_request
        do_POST = do_request
        do_PUT = do_request
        do_PATCH = do_request
        do_DELETE = do_request
        do_HEAD = do_request
        do_OPTIONS = do_request

        def log_message(self, format, *args):
            logger.debug("api-proxy: %s", format % args)

    return ProxyHandler


class APIProxy:
    """A local reverse proxy that forwards to an API via an upstream proxy."""

    def __init__(
        self,
        target_url: str,
        http_proxy: str | None = None,
        https_proxy: str | None = None,
        host: str = "0.0.0.0",
        port: int | None = None,
        injected_headers: dict[str, str] | None = None,
    ) -> None:
        self.target_url = target_url.rstrip("/")
        parsed_target = urlparse(self.target_url)
        if parsed_target.scheme not in ("http", "https") or not parsed_target.hostname:
            raise ValueError("API proxy target must be an absolute HTTP(S) URL")
        if parsed_target.username is not None or parsed_target.password is not None:
            raise ValueError("API proxy target must not contain credentials")
        self.host = host
        self.port = port or _find_free_port(host)

        self._session = req_lib.Session()
        proxies: dict[str, str] = {}
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        self._session.proxies = proxies

        self._client_policy: dict[str, set[str] | None] = {"allowed_ips": None}
        handler = _make_handler(
            self.target_url,
            self._session,
            injected_headers=injected_headers,
            client_policy=self._client_policy,
        )
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread: threading.Thread | None = None

    @property
    def local_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        """Start the proxy in a background thread. Returns the local URL."""
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="sforge-api-proxy",
        )
        self._thread.start()
        logger.info(
            f"API proxy started: {self.local_url} -> {self.target_url} "
            f"(via proxy)"
        )
        return self.local_url

    def restrict_clients(self, allowed_ips: set[str]) -> None:
        """Restrict requests to exact client IPs before serving traffic."""
        if self._thread is not None:
            raise RuntimeError("API proxy client policy cannot change after start")
        if not allowed_ips:
            raise ValueError("API proxy requires at least one allowed client IP")
        self._client_policy["allowed_ips"] = set(allowed_ips)

    def run_forever(self) -> None:
        """Run the proxy in the foreground (blocks until SIGINT/SIGTERM)."""
        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)

        def _shutdown(signum, frame):
            print("\nShutting down API proxy...")
            threading.Thread(
                target=self._server.shutdown, daemon=True
            ).start()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
        try:
            self._server.serve_forever()
        finally:
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)

    def stop(self) -> None:
        if self._thread:
            self._server.shutdown()
            self._thread.join(timeout=5)
        self._server.server_close()
        self._session.close()
        logger.info("API proxy stopped")
