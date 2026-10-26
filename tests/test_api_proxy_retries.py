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

from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time

import pytest
import requests

from sforge.harness import api_proxy
from sforge.harness.config import load_config


@contextmanager
def server(responses, *, retry_seconds=0):
    calls = []
    observed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            calls.append((self.path, body, self.headers.get('Authorization')))
            status, headers, payload = responses[min(len(calls) - 1, len(responses) - 1)]
            self.send_response(status)
            self.send_header('Content-Length', str(len(payload)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)
            observed.set()

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    proxy = api_proxy.APIProxy(
        f'http://127.0.0.1:{upstream.server_port}/v1', host='127.0.0.1',
        injected_headers={'Authorization': 'Bearer synthetic'},
        rate_limit_retry_seconds=retry_seconds,
    )
    proxy._session.trust_env = False
    proxy.start()
    client = requests.Session()
    client.trust_env = False
    try:
        yield proxy, client, calls, observed
    finally:
        client.close()
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        thread.join(5)


def test_default_forwards_429_unchanged_without_retry():
    response = (429, {'Retry-After': '7'}, b'limited')
    with server([response]) as (proxy, client, calls, _):
        result = client.post(proxy.local_url + '/responses', data=b'prompt', timeout=3)
        assert (result.status_code, result.content, result.headers['Retry-After']) == (429, b'limited', '7')
        assert len(calls) == 1


def test_nine_429s_recover_without_changing_request_or_stream(caplog):
    caplog.set_level('INFO', logger=api_proxy.__name__)
    replies = [(429, {'Retry-After': '0'}, b'limited')] * 9
    sse = b'data: first\n\ndata: final\n\n'
    replies.append((200, {'Content-Type': 'text/event-stream'}, sse))
    with server(replies, retry_seconds=30) as (proxy, client, calls, _):
        result = client.post(proxy.local_url + '/responses?x=1', data=b'original', timeout=15)
        assert result.status_code == 200
        assert result.content == sse
        assert calls == [('/v1/responses?x=1', b'original', 'Bearer synthetic')] * 10
    assert 'rate_limit_retries=9' in caplog.text
    assert 'original' not in caplog.text
    assert 'Bearer synthetic' not in caplog.text


@pytest.mark.parametrize('status', [400, 401, 403, 500])
def test_other_http_errors_are_not_retried(status):
    with server([(status, {}, b'error')], retry_seconds=10) as (proxy, client, calls, _):
        assert client.post(proxy.local_url + '/responses', timeout=3).status_code == status
        assert len(calls) == 1


def test_retry_after_beyond_budget_is_not_shortened():
    with server([(429, {'Retry-After': '30'}, b'limited')], retry_seconds=1) as (proxy, client, calls, _):
        result = client.post(proxy.local_url + '/responses', timeout=3)
        assert result.status_code == 429
        assert result.headers['Retry-After'] == '30'
        assert result.content == b'limited'
        assert len(calls) == 1


@pytest.mark.parametrize('cancel', ['client', 'trial'])
def test_cancellation_during_backoff_prevents_next_request(cancel):
    with server([(429, {'Retry-After': '1'}, b'limited')], retry_seconds=10) as (proxy, _, calls, observed):
        connection = socket.create_connection(('127.0.0.1', proxy.port), timeout=3)
        connection.sendall(b'POST /responses HTTP/1.0\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n')
        assert observed.wait(3)
        if cancel == 'trial':
            proxy.stop()
        connection.close()
        time.sleep(1.2)
        assert len(calls) == 1


def test_retry_after_parsing_and_bounded_fallback(monkeypatch):
    monkeypatch.setattr(api_proxy.time, 'time', lambda: 1000)
    monkeypatch.setattr(api_proxy.random, 'uniform', lambda a, b: 1)
    date = format_datetime(datetime.fromtimestamp(1060, timezone.utc), usegmt=True)
    assert api_proxy._rate_limit_delay('12', 1) == 12
    assert api_proxy._rate_limit_delay(date, 1) == 60
    assert api_proxy._rate_limit_delay(None, 1) == 5
    assert api_proxy._rate_limit_delay(None, 1000) == 60
    for value in ['bad', 'nan', 'inf', '-1']:
        assert api_proxy._rate_limit_delay(value, 2) == 10


def test_retry_window_expires_and_preserves_last_response():
    with server([(429, {'Retry-After': '0'}, b'limited')], retry_seconds=2) as (proxy, client, calls, _):
        before = time.monotonic()
        result = client.post(proxy.local_url + '/responses', timeout=3)
        assert time.monotonic() - before < 2
        assert result.status_code == 429 and result.content == b'limited'
        assert 2 <= len(calls) <= 11


def test_config_environment(monkeypatch):
    monkeypatch.setenv('SFORGE_AGENT_RATE_LIMIT_RETRY_SECONDS', '900')
    assert load_config().agent_rate_limit_retry_seconds == 900


@pytest.mark.parametrize('value', [-1, True, 1.5, '900'])
def test_invalid_retry_budget(value):
    with pytest.raises(ValueError, match='nonnegative integer'):
        api_proxy.APIProxy('http://127.0.0.1:12345', rate_limit_retry_seconds=value)
