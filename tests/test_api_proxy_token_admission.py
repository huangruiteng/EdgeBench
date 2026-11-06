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
import multiprocessing
import os
import socket
import sqlite3
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

import sforge.harness.token_admission as token_admission
from sforge.harness.api_proxy import APIProxy
from sforge.harness.config import load_config
from sforge.harness.token_admission import TokenAdmissionLedger


def _direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


class _ControlledProvider:
    def __init__(
        self,
        usages: list[dict[str, int] | None],
        statuses: list[int] | None = None,
        headers: list[dict[str, str]] | None = None,
    ) -> None:
        self.usages = usages
        self.statuses = statuses or [200] * len(usages)
        self.headers = headers or [{} for _ in usages]
        self.calls = 0
        self.first_arrived = threading.Event()
        self.release_first = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                call = owner.calls
                owner.calls += 1
                if call == 0:
                    owner.first_arrived.set()
                    owner.release_first.wait(timeout=5)
                payload: dict[str, object] = {"id": f"response-{call}"}
                usage = owner.usages[min(call, len(owner.usages) - 1)]
                if usage is not None:
                    payload["usage"] = usage
                encoded = json.dumps(payload).encode()
                status = owner.statuses[min(call, len(owner.statuses) - 1)]
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                if status == 429:
                    self.send_header("Retry-After", "0")
                for key, value in owner.headers[min(call, len(owner.headers) - 1)].items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.release_first.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _proxy(
    provider: _ControlledProvider,
    ledger,
    *,
    limit=100,
    reservation=80,
    retry_seconds=0,
) -> APIProxy:
    proxy = APIProxy(
        provider.url,
        host="127.0.0.1",
        token_ledger_path=ledger,
        token_limit_per_minute=limit,
        max_tokens_per_request=reservation,
        rate_limit_retry_seconds=retry_seconds,
    )
    proxy._session.trust_env = False
    proxy.start()
    return proxy


def _post_in_thread(proxy: APIProxy, results: list[int]) -> threading.Thread:
    def send() -> None:
        with _direct_session() as client:
            results.append(client.post(proxy.local_url, json={"input": "x"}, timeout=5).status_code)

    thread = threading.Thread(target=send)
    thread.start()
    return thread


def _reserve_from_process(path: str, gate, results) -> None:
    ledger = TokenAdmissionLedger(path, 100, 30)
    gate.wait(timeout=5)
    results.put(ledger.try_reserve() is not None)


@pytest.mark.parametrize("mode", [0o720, 0o702])
def test_token_ledger_rejects_group_or_world_writable_parent(tmp_path, mode):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    parent.chmod(mode)

    with pytest.raises(ValueError, match="private owned directory"):
        TokenAdmissionLedger(parent / "tokens.sqlite3", 100, 30)


def test_token_ledger_rejects_symlink(tmp_path):
    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    path = tmp_path / "tokens.sqlite3"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        TokenAdmissionLedger(path, 100, 30)


def test_token_ledger_creation_is_private_under_permissive_umask(tmp_path):
    path = tmp_path / "tokens.sqlite3"
    previous = os.umask(0)
    try:
        TokenAdmissionLedger(path, 100, 30)
    finally:
        os.umask(previous)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_token_ledger_rejects_path_swap_during_sqlite_connect(tmp_path, monkeypatch):
    path = tmp_path / "tokens.sqlite3"
    displaced = tmp_path / "original.sqlite3"
    original_connect = token_admission.sqlite3.connect
    swapped = False

    def swapping_connect(database, *args, **kwargs):
        nonlocal swapped
        if Path(database) == path and not swapped:
            swapped = True
            path.rename(displaced)
            path.touch(mode=0o600)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(token_admission.sqlite3, "connect", swapping_connect)

    with pytest.raises(ValueError, match="changed while opening"):
        TokenAdmissionLedger(path, 100, 30)
    assert swapped


def test_shared_ledger_waits_before_dispatch_then_reconciles_actual_usage(tmp_path):
    provider = _ControlledProvider([
        {"input_tokens": 15, "output_tokens": 5, "total_tokens": 20},
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    ])
    ledger = tmp_path / "shared-tokens.sqlite3"
    first = _proxy(provider, ledger)
    second = _proxy(provider, ledger)
    results: list[int] = []
    first_request = _post_in_thread(first, results)
    assert provider.first_arrived.wait(timeout=2)
    second_request = _post_in_thread(second, results)
    try:
        time.sleep(0.2)
        assert provider.calls == 1
        provider.release_first.set()
        first_request.join(timeout=3)
        second_request.join(timeout=3)
        assert not first_request.is_alive() and not second_request.is_alive()
        assert provider.calls == 2
        assert results == [200, 200]
    finally:
        first.close_admission()
        second.close_admission()
        first.stop()
        second.stop()
        provider.close()


def test_completed_usage_stays_in_window_and_prevents_over_limit_dispatch(tmp_path):
    provider = _ControlledProvider([
        {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
    ])
    ledger = tmp_path / "shared-tokens.sqlite3"
    first = _proxy(provider, ledger)
    second = _proxy(provider, ledger)
    first_result: list[int] = []
    second_result: list[int] = []
    first_request = _post_in_thread(first, first_result)
    assert provider.first_arrived.wait(timeout=2)
    provider.release_first.set()
    first_request.join(timeout=3)
    assert first_result == [200]
    blocked = _post_in_thread(second, second_result)
    try:
        time.sleep(0.2)
        assert provider.calls == 1
        second.close_admission()
        blocked.join(timeout=3)
        assert second_result == [503]
        assert provider.calls == 1
    finally:
        first.close_admission()
        second.close_admission()
        first.stop()
        second.stop()
        provider.close()


def test_missing_usage_keeps_full_conservative_charge(tmp_path):
    provider = _ControlledProvider([None])
    ledger = tmp_path / "shared-tokens.sqlite3"
    first = _proxy(provider, ledger)
    second = _proxy(provider, ledger)
    first_result: list[int] = []
    blocked_result: list[int] = []
    first_request = _post_in_thread(first, first_result)
    assert provider.first_arrived.wait(timeout=2)
    provider.release_first.set()
    first_request.join(timeout=3)
    assert first_result == [200]
    blocked = _post_in_thread(second, blocked_result)
    try:
        time.sleep(0.2)
        assert provider.calls == 1
        second.close_admission()
        blocked.join(timeout=3)
        assert blocked_result == [503]
    finally:
        first.close_admission()
        second.close_admission()
        first.stop()
        second.stop()
        provider.close()


def test_429_releases_reservation_before_retry(tmp_path):
    provider = _ControlledProvider(
        [None, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}],
        statuses=[429, 200],
    )
    provider.release_first.set()
    proxy = _proxy(
        provider,
        tmp_path / "shared-tokens.sqlite3",
        limit=80,
        reservation=80,
        retry_seconds=3,
    )
    try:
        with _direct_session() as client:
            response = client.post(proxy.local_url, json={"input": "x"}, timeout=5)
        assert response.status_code == 200
        assert provider.calls == 2
    finally:
        proxy.close_admission()
        proxy.stop()
        provider.close()


def test_token_admission_does_not_follow_redirect_inside_one_reservation(tmp_path):
    provider = _ControlledProvider(
        [None, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}],
        statuses=[307, 200],
        headers=[{"Location": "/redirected"}, {}],
    )
    provider.release_first.set()
    proxy = _proxy(provider, tmp_path / "shared-tokens.sqlite3")
    try:
        with _direct_session() as client:
            response = client.post(
                proxy.local_url, json={"input": "x"}, timeout=5, allow_redirects=False
            )
        assert response.status_code == 307
        assert provider.calls == 1
    finally:
        proxy.close_admission()
        proxy.stop()
        provider.close()


def test_client_disconnect_during_reservation_never_dispatches(tmp_path, monkeypatch):
    provider = _ControlledProvider([
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    ])
    proxy = _proxy(provider, tmp_path / "shared-tokens.sqlite3")
    entered = threading.Event()
    release = threading.Event()
    original = proxy._token_ledger.try_reserve

    def delayed_reservation():
        entered.set()
        assert release.wait(timeout=3)
        return original()

    monkeypatch.setattr(proxy._token_ledger, "try_reserve", delayed_reservation)
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=3)
    connection.sendall(
        b"POST /responses HTTP/1.0\r\nHost: local\r\n"
        b"Content-Length: 2\r\n\r\n{}"
    )
    assert entered.wait(timeout=2)
    connection.close()
    release.set()
    try:
        time.sleep(0.3)
        assert provider.calls == 0
    finally:
        proxy.close_admission()
        proxy.stop()
        provider.close()


def test_cross_process_admission_is_linearized_and_dead_owners_stay_charged(tmp_path):
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    results = context.Queue()
    path = str(tmp_path / "shared-tokens.sqlite3")
    processes = [
        context.Process(target=_reserve_from_process, args=(path, gate, results))
        for _ in range(12)
    ]
    for process in processes:
        process.start()
    gate.set()
    admitted = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sum(admitted) == 3

    # Reconciliation treats a dead process as unknown provider usage, not free
    # capacity. That conservative record expires through the normal 60s window.
    ledger = TokenAdmissionLedger(path, 100, 30)
    assert ledger.try_reserve() is None
    with sqlite3.connect(path) as connection:
        records = connection.execute(
            "SELECT state, actual_tokens, actual_known FROM token_records"
        ).fetchall()
    assert records == [("accounted", 30, 0)] * 3


def test_completed_charge_is_released_after_rolling_window(tmp_path):
    path = tmp_path / "shared-tokens.sqlite3"
    ledger = TokenAdmissionLedger(path, 100, 80)
    reservation_id = ledger.try_reserve()
    assert reservation_id is not None
    assert ledger.settle(reservation_id, 30)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE token_records SET accounted_at = ? WHERE reservation_id = ?",
            (time.time() - 61, reservation_id),
        )
    assert ledger.try_reserve() is not None


def test_token_admission_config_loads_host_only_environment(monkeypatch, tmp_path):
    ledger = tmp_path / "tokens.sqlite3"
    monkeypatch.setenv("SFORGE_AGENT_TOKEN_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("SFORGE_AGENT_TOKEN_LIMIT_PER_MINUTE", "6000000")
    monkeypatch.setenv("SFORGE_AGENT_MAX_TOKENS_PER_REQUEST", "1000000")
    config = load_config()
    assert config.agent_token_ledger_path == ledger
    assert config.agent_token_limit_per_minute == 6_000_000
    assert config.agent_max_tokens_per_request == 1_000_000
