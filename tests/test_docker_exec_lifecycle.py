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

"""Opt-in Docker regressions; use a cached image with bash, Python and procps."""

import os
import threading
import time

import docker
import pytest
import requests

from sforge.harness import docker_utils
from sforge.harness.backend.docker_backend import DockerBackend, DockerContainerHandle


@pytest.fixture
def container():
    image = os.environ.get("SFORGE_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("set SFORGE_DOCKER_TEST_IMAGE to a cached bash/python3/procps image")
    client = docker.from_env()
    client.images.get(image)  # Never pull an image from this test.
    owned = client.containers.run(
        image,
        ["sleep", "120"],
        detach=True,
        network_disabled=True,
        labels={"test": "sforge-exec-lifecycle"},
    )
    try:
        yield owned
    finally:
        try:
            owned.remove(force=True)
        except docker.errors.NotFound:
            pass  # A disappearance regression may have removed this exact ID.
        assert not client.containers.list(all=True, filters={"id": owned.id})
        client.close()


@pytest.fixture(params=["legacy", "backend"])
def execute(request, container):
    def run(command, **kwargs):
        if request.param == "legacy":
            return docker_utils._exec_run(container, command, **kwargs)
        result = DockerBackend(container.client).exec_run_with_timeout(
            DockerContainerHandle(container), command, **kwargs
        )
        return result.output, result.exit_code, result.timed_out, result.elapsed_seconds

    return run


def test_timeout_stops_before_return_and_drains_final_output(
    container, execute, tmp_path
):
    body = """import signal,time,sys,os
open('/tmp/solver.pid','w').write(str(os.getpid()))
def stop(*_):
    time.sleep(.2)
    open('/tmp/submission','w').write('final')
    print('FINAL_RECEIPT',flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM,stop)
print('READY',flush=True)
time.sleep(60)
"""
    log = tmp_path / "agent.log"
    output, code, timed_out, elapsed = execute(
        ["python3", "-u", "-c", body], timeout=1, log_file=log
    )
    assert timed_out and code == -1
    assert "FINAL_RECEIPT" in output
    assert log.read_bytes() == output.encode()
    probe = container.exec_run(
        [
            "python3",
            "-c",
            "from pathlib import Path; p=Path('/tmp/solver.pid').read_text(); assert not Path('/proc/'+p).exists(); assert Path('/tmp/submission').read_text()=='final'",
        ]
    )
    assert probe.exit_code == 0, probe.output
    assert elapsed >= 1


def test_cancel_during_create_does_not_dispatch(container, execute, monkeypatch):
    stop = threading.Event()
    create = container.client.api.exec_create
    started = []
    start = container.client.api.exec_start

    def after_create(*args, **kwargs):
        result = create(*args, **kwargs)
        if "sforge-exec-supervisor" in str(args):
            stop.set()
        return result

    def record_start(exec_id, **kwargs):
        started.append(exec_id)
        return start(exec_id, **kwargs)

    monkeypatch.setattr(container.client.api, "exec_create", after_create)
    monkeypatch.setattr(container.client.api, "exec_start", record_start)
    output, code, timed_out, _ = execute(
        ["touch", "/tmp/dispatched"], timeout=5, shutdown_event=stop
    )
    # Only provisioning/cleanup control execs may start. No solver invocation.
    solver_starts = [
        x
        for x in started
        if container.client.api.exec_inspect(x)["ProcessConfig"]["arguments"][1:2]
        == [docker_utils._EXEC_SUPERVISOR]
    ]
    assert not solver_starts
    assert output == "" and code == -1 and timed_out
    assert container.exec_run(["test", "!", "-e", "/tmp/dispatched"]).exit_code == 0


@pytest.mark.parametrize("user", [None, "65534"])
@pytest.mark.parametrize("timeout_exit", [False, True])
def test_task_temp_cleanup_preserves_exec_receipts(container, execute, user, timeout_exit):
    # A normal verifier may clear scratch files before running its test suite.
    # Use a dedicated network-disabled container; never touch host temp files.
    script = 'find /tmp /var/tmp -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; '
    script += (
        'trap \'printf "STOPPED\\n"; exit 0\' TERM; sleep 60 & wait'
        if timeout_exit else 'printf "COMPLETED\\n"; exit 19'
    )
    output, code, timed_out, _ = execute(
        ["bash", "-c", script], timeout=1 if timeout_exit else 5, user=user,
    )
    assert timed_out is timeout_exit
    assert code == (-1 if timeout_exit else 19)
    assert ("STOPPED" if timeout_exit else "COMPLETED") in output
    assert execute(["printf", "NEXT"], timeout=5, user=user)[:3] == ("NEXT", 0, False)


@pytest.mark.parametrize("exit_code", [0, 17])
def test_normal_exit_preserves_bytes_arguments_and_unrelated_process(
    container, execute, tmp_path, exit_code
):
    neighbor = container.client.api.exec_create(container.id, ["sleep", "90"])["Id"]
    container.client.api.exec_start(neighbor, detach=True)
    assert container.client.api.exec_inspect(neighbor)["Running"]
    script = r"""import os,sys
assert os.getcwd()=='/tmp'
assert os.environ['EXAMPLE_VALUE']==sys.argv[1]
sys.stdout.buffer.write(b'hello\xff\xe4\xb8\xad\n')
sys.stderr.write('stderr\n')
sys.exit(int(sys.argv[2]))
"""
    value = "literal $HOME `cmd` ' quote \n"
    chunks = []
    log = tmp_path / "output"
    log.write_bytes(b"prior\n")
    result = execute(
        ["python3", "-c", script, value, str(exit_code)],
        timeout=5,
        workdir="/tmp",
        environment={"EXAMPLE_VALUE": value},
        log_file=log,
        log_append=True,
        on_chunk=chunks.append,
    )
    assert result[1:3] == (exit_code, False)
    assert b"hello\xff\xe4\xb8\xad\n" in b"".join(chunks)
    assert result[0] == b"".join(chunks).decode(errors="replace")
    assert log.read_bytes() == b"prior\n" + b"".join(chunks)
    probe = container.exec_run(
        [
            "python3",
            "-c",
            "from pathlib import Path; assert sum(p.read_bytes()==b'sleep\\x0090\\x00' for p in Path('/proc').glob('[0-9]*/cmdline'))==1",
        ]
    )
    assert probe.exit_code == 0, probe.output
    assert (
        container.exec_run(["bash", "-c", 'compgen -G "/dev/shm/sforge-exec-*"']).exit_code
        == 1
    )


def test_explicit_cancellation_drains_shutdown(container, execute, tmp_path):
    stop = threading.Event()
    chunks = []

    def on_chunk(chunk):
        chunks.append(chunk)
        if b"READY" in chunk:
            stop.set()

    body = """import signal,time,sys
def stop(*_):
    time.sleep(.1)
    print('STOPPED',flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM,stop)
print('READY',flush=True)
time.sleep(60)
"""
    result = execute(
        ["python3", "-u", "-c", body],
        timeout=None,
        shutdown_event=stop,
        on_chunk=on_chunk,
        log_file=tmp_path / "out",
    )
    assert result[1:3] == (-1, True)
    assert result[0] == "READY\nSTOPPED\n"
    assert (tmp_path / "out").read_text() == result[0]
    assert b"".join(chunks).decode() == result[0]


def test_force_stop_closed_output_and_child_process(container, execute, monkeypatch):
    monkeypatch.setattr(docker_utils, "_EXEC_SHUTDOWN_GRACE", 1)
    body = """import os,signal,time,subprocess
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
p=subprocess.Popen(['sleep','60'])
Path('/tmp/owned').write_text(str(os.getpid())+' '+str(p.pid))
os.close(1);os.close(2)
time.sleep(60)
"""
    result = execute(["python3", "-c", body], timeout=1)
    assert result[1:3] == (-1, True)
    assert result[3] < 5
    probe = container.exec_run(
        [
            "python3",
            "-c",
            "from pathlib import Path; ids=Path('/tmp/owned').read_text().split(); assert all(not Path('/proc/'+p+'/stat').exists() or Path('/proc/'+p+'/stat').read_text().split(') ')[1].split()[0]=='Z' for p in ids)",
        ]
    )
    assert probe.exit_code == 0, probe.output


def test_normal_parent_exit_cleans_background_pipe_holder(
    container, execute, monkeypatch
):
    monkeypatch.setattr(docker_utils, "_EXEC_SHUTDOWN_GRACE", 1)
    body = """import subprocess
from pathlib import Path
p=subprocess.Popen(['sleep','60'])
Path('/tmp/child').write_text(str(p.pid))
print('DONE',flush=True)
"""
    result = execute(["python3", "-c", body], timeout=5)
    assert result[0] == "DONE\n" and result[1:3] == (0, False)
    assert result[3] < 3
    probe = container.exec_run(
        [
            "python3",
            "-c",
            "from pathlib import Path; p=Path('/tmp/child').read_text(); s=Path('/proc/'+p+'/stat'); assert not s.exists() or s.read_text().split(') ')[1].split()[0]=='Z'",
        ]
    )
    assert probe.exit_code == 0, probe.output


def test_command_can_kill_its_matching_process_without_killing_supervisor(
    container, execute
):
    result = execute(
        [
            "bash",
            "-c",
            (
                "ps -eo pid=,args= | grep -E '[b]enchmark-worker' | "
                "awk '{print $1}' | while read -r pid; do kill -9 \"$pid\"; done"
            ),
            "benchmark-worker",
        ],
        timeout=5,
    )
    assert result[1:3] == (137, False)

    follow_up = execute(["printf", "AFTER_SELF_KILL"], timeout=5)
    assert follow_up[0] == "AFTER_SELF_KILL"
    assert follow_up[1:3] == (0, False)


def test_nonroot_and_string_command(container, execute):
    result = execute('printf "literal space"', user="65534", timeout=5)
    assert result[0] == "literal space" and result[1:3] == (0, False)


def test_preset_cancellation_creates_nothing(container, execute, monkeypatch):
    stop = threading.Event()
    stop.set()

    def unexpected(*args, **kwargs):
        raise AssertionError("must not create or dispatch an exec")

    monkeypatch.setattr(container.client.api, "exec_create", unexpected)
    result = execute(["touch", "/tmp/unexpected"], shutdown_event=stop)
    assert result[:3] == ("", -1, True)


def test_start_failure_keeps_original_error(container, execute, monkeypatch):
    class StartFailure(Exception):
        pass

    original = container.client.api.exec_start
    monkeypatch.setattr(docker_utils, "_EXEC_SHUTDOWN_GRACE", 1)
    monkeypatch.setattr(docker_utils, "_EXEC_DRAIN_GRACE", 1)

    def fail_start(exec_id, **kwargs):
        if kwargs.get("stream"):
            raise StartFailure("synthetic start rejection")
        return original(exec_id, **kwargs)

    monkeypatch.setattr(container.client.api, "exec_start", fail_start)
    with pytest.raises(StartFailure, match="synthetic start rejection"):
        execute(["touch", "/tmp/dispatched"], timeout=3)
    assert container.exec_run(["test", "!", "-e", "/tmp/dispatched"]).exit_code == 0


def test_stream_failure_stops_command_and_preserves_error(
    container, execute, monkeypatch, tmp_path
):
    class StreamFailure(Exception):
        pass

    original = container.client.api.exec_start

    def fail_stream(exec_id, **kwargs):
        stream = original(exec_id, **kwargs)
        if not kwargs.get("stream"):
            return stream

        def chunks():
            try:
                yield next(stream)
                raise StreamFailure("synthetic read failure")
            finally:
                stream.close()

        return chunks()

    monkeypatch.setattr(container.client.api, "exec_start", fail_stream)
    with pytest.raises(StreamFailure, match="synthetic read failure"):
        execute(
            ["bash", "-c", "echo $$ > /tmp/solver.pid; echo READY; sleep 60"],
            timeout=5,
            log_file=tmp_path / "log",
        )
    assert (tmp_path / "log").read_text() == "READY\n"
    probe = container.exec_run(
        [
            "python3",
            "-c",
            "from pathlib import Path; p=Path('/tmp/solver.pid').read_text().strip(); assert not Path('/proc/'+p).exists()",
        ]
    )
    assert probe.exit_code == 0, probe.output


def test_missing_shutdown_receipt_is_not_success(container, execute, monkeypatch):
    monkeypatch.setattr(docker_utils, "_EXEC_SUPERVISOR", "exit 0")
    with pytest.raises(RuntimeError, match="shutdown"):
        execute(["touch", "/tmp/dispatched"], timeout=2)
    assert container.exec_run(["test", "!", "-e", "/tmp/dispatched"]).exit_code == 0


@pytest.mark.parametrize("exit_code", [0, 19])
def test_missing_exec_metadata_recovers_from_settled_receipt(
    container, execute, monkeypatch, exit_code
):
    create = container.client.api.exec_create
    inspect = container.client.api.exec_inspect
    main_exec = {}

    def remember_main(*args, **kwargs):
        result = create(*args, **kwargs)
        if "sforge-exec-supervisor" in str(args):
            main_exec["id"] = result["Id"]
        return result

    def forget_completed_main(exec_id):
        if exec_id == main_exec.get("id"):
            raise docker.errors.NotFound("synthetic expired exec metadata")
        return inspect(exec_id)

    monkeypatch.setattr(container.client.api, "exec_create", remember_main)
    monkeypatch.setattr(container.client.api, "exec_inspect", forget_completed_main)
    result = execute(["bash", "-c", f"printf RECOVERED; exit {exit_code}"], timeout=5)
    assert result[:3] == ("RECOVERED", exit_code, False)
    assert (
        container.exec_run(["bash", "-c", 'compgen -G "/dev/shm/sforge-exec-*"']).exit_code
        == 1
    )


@pytest.mark.parametrize("exit_code", [0, 23])
def test_missing_exec_metadata_retries_transient_receipt_timeout(
    container, execute, monkeypatch, exit_code
):
    create = container.client.api.exec_create
    inspect = container.client.api.exec_inspect
    control = container.exec_run
    main_exec = {}
    receipt_reads = 0

    def remember_main(*args, **kwargs):
        result = create(*args, **kwargs)
        if "sforge-exec-supervisor" in str(args):
            main_exec["id"] = result["Id"]
        return result

    def forget_completed_main(exec_id):
        if exec_id == main_exec.get("id"):
            raise docker.errors.NotFound("synthetic expired exec metadata")
        return inspect(exec_id)

    def timeout_first_receipt(command, **kwargs):
        nonlocal receipt_reads
        if 'cat -- "$1/settled"' in str(command):
            receipt_reads += 1
            if receipt_reads == 1:
                raise requests.ReadTimeout("synthetic Docker daemon timeout")
        return control(command, **kwargs)

    monkeypatch.setattr(container.client.api, "exec_create", remember_main)
    monkeypatch.setattr(container.client.api, "exec_inspect", forget_completed_main)
    monkeypatch.setattr(container, "exec_run", timeout_first_receipt)
    result = execute(["bash", "-c", f"printf RECOVERED; exit {exit_code}"], timeout=5)
    assert result[:3] == ("RECOVERED", exit_code, False)
    assert receipt_reads >= 2
    assert (
        container.exec_run(["bash", "-c", 'compgen -G "/dev/shm/sforge-exec-*"']).exit_code
        == 1
    )


def test_cancel_before_remote_start_preserves_no_dispatch(
    container, execute, monkeypatch
):
    released = threading.Event()
    start = container.client.api.exec_start
    control = container.exec_run

    def delayed_start(exec_id, **kwargs):
        if kwargs.get("stream"):
            assert released.wait(3), "stop request was not sent"
        return start(exec_id, **kwargs)

    def release_on_stop(command, **kwargs):
        result = control(command, **kwargs)
        if ': > "$1/stop"' in command:
            released.set()
        return result

    monkeypatch.setattr(container.client.api, "exec_start", delayed_start)
    monkeypatch.setattr(container, "exec_run", release_on_stop)
    result = execute(["touch", "/tmp/dispatched"], timeout=0.2)
    assert result[:3] == ("", -1, True)
    assert container.exec_run(["test", "!", "-e", "/tmp/dispatched"]).exit_code == 0


def test_keyboard_interrupt_stops_before_propagating(
    container, execute, monkeypatch, tmp_path
):
    join = threading.Thread.join
    interrupted = False

    def interrupt_once(thread, *args, **kwargs):
        nonlocal interrupted
        if thread.name == "sforge-docker-exec" and not interrupted:
            interrupted = True
            # Observe startup before injecting the host interruption.
            assert ready.wait(3)
            raise KeyboardInterrupt("synthetic host cancel")
        return join(thread, *args, **kwargs)

    ready = threading.Event()
    monkeypatch.setattr(threading.Thread, "join", interrupt_once)
    with pytest.raises(KeyboardInterrupt, match="synthetic host cancel"):
        execute(
            ["bash", "-c", "echo $$ > /tmp/solver.pid; echo READY; sleep 60"],
            timeout=5,
            log_file=tmp_path / "out",
            on_chunk=lambda _: ready.set(),
        )
    probe = container.exec_run(
        [
            "python3",
            "-c",
            "from pathlib import Path; p=Path('/tmp/solver.pid').read_text().strip(); assert not Path('/proc/'+p).exists()",
        ]
    )
    assert probe.exit_code == 0, probe.output
    assert (tmp_path / "out").read_text() == "READY\n"


def test_public_exit_code_api(container):
    result = docker_utils.exec_run_with_exit_code(
        container, ["bash", "-c", "printf original; exit 7"]
    )
    assert result[:3] == ("original", 7, False)
    result = DockerBackend(container.client).exec_run_with_exit_code(
        DockerContainerHandle(container), ["bash", "-c", "printf original; exit 7"]
    )
    assert (result.output, result.exit_code, result.timed_out) == ("original", 7, False)
    result = docker_utils.exec_run_with_timeout(container, ["printf", "original"])
    assert result[0:2] == ("original", False) and len(result) == 3


@pytest.fixture
def slow_receipt_publication(monkeypatch):
    # Redirection opens the destination before the builtin writes bytes. Widen
    # that window without a child process: a sleeping child would itself keep
    # group_has_live true and accidentally hide premature anchor termination.
    marker = "    code=$?\n"
    supervisor = docker_utils._EXEC_SUPERVISOR
    assert supervisor.count(marker) == 1
    delay = r"""
    printf() {
        builtin printf "PUBLISHING\n" >&2
        local end=$((SECONDS + 2))
        while (( SECONDS < end )); do :; done
        builtin printf "$@"
    }
"""
    monkeypatch.setattr(
        docker_utils, "_EXEC_SUPERVISOR", supervisor.replace(marker, marker + delay)
    )


@pytest.mark.parametrize("exit_code", [0, 17])
def test_slow_receipt_publication_preserves_exit_code(
    container, execute, slow_receipt_publication, exit_code
):
    result = execute(["bash", "-c", f"printf DONE; exit {exit_code}"], timeout=10)
    # Docker multiplexes stdout and stderr independently, so their cross-stream
    # ordering is not stable even though both streams are fully drained.
    assert result[0] in ("DONEPUBLISHING\n", "PUBLISHING\nDONE")
    assert result[1:3] == (exit_code, False)
    assert (
        container.exec_run(["bash", "-c", 'compgen -G "/dev/shm/sforge-exec-*"']).exit_code
        == 1
    )


def test_cancel_during_receipt_publication_cleans_partial_file(
    container, execute, slow_receipt_publication
):
    stop = threading.Event()

    def on_chunk(chunk):
        if b"PUBLISHING" in chunk:
            stop.set()

    result = execute(["true"], timeout=10, shutdown_event=stop, on_chunk=on_chunk)
    assert result[1:3] == (-1, True)
    assert stop.is_set()
    assert (
        container.exec_run(["bash", "-c", 'compgen -G "/dev/shm/sforge-exec-*"']).exit_code
        == 1
    )


def test_receipt_publication_failure_is_nonzero(container, execute, monkeypatch):
    supervisor = docker_utils._EXEC_SUPERVISOR
    marker = "    code=$?\n"
    assert supervisor.count(marker) == 1
    monkeypatch.setattr(
        docker_utils,
        "_EXEC_SUPERVISOR",
        supervisor.replace(marker, marker + "    mv() { return 1; }\n"),
    )
    result = execute(["printf", "DONE"], timeout=5)
    assert result[:3] == ("DONE", 125, False)
    assert (
        container.exec_run(["bash", "-c", 'compgen -G "/dev/shm/sforge-exec-*"']).exit_code
        == 1
    )


@pytest.mark.parametrize('cancel', [False, True])
@pytest.mark.parametrize('user', [None, '65534'])
def test_snapshot_pause_across_stop_preserves_terminal_and_drained_output(
    container, execute, tmp_path, monkeypatch, cancel, user
):
    ready = threading.Event()
    stop_attempt = threading.Event()
    stop = threading.Event()
    failures = []
    paused_errors = []
    real_control = container.exec_run

    def record_control(command, **kwargs):
        is_stop = command[2] == ': > "$1/stop"'
        if is_stop:
            stop_attempt.set()
        try:
            return real_control(command, **kwargs)
        except docker.errors.APIError as exc:
            if is_stop:
                paused_errors.append(exc.status_code)
            raise

    def snapshot():
        try:
            assert ready.wait(5)
            container.pause()
            if cancel:
                stop.set()
            assert stop_attempt.wait(5)
            time.sleep(.3)
        except BaseException as exc:
            failures.append(exc)
        finally:
            container.reload()
            if container.attrs['State']['Paused']:
                container.unpause()  # Only the snapshot owner resumes it.

    monkeypatch.setattr(container, 'exec_run', record_control)
    observer = threading.Thread(target=snapshot)
    observer.start()
    body = '''import signal,time,sys
signal.signal(signal.SIGTERM, lambda *_: (print('FINAL',flush=True),sys.exit(0)))
print('READY',flush=True)
time.sleep(60)
'''
    log = tmp_path / 'paused.log'
    try:
        result = execute(
            ['python3', '-u', '-c', body], timeout=2,
            user=user, shutdown_event=stop, log_file=log,
            on_chunk=lambda chunk: ready.set() if b'READY' in chunk else None,
        )
    finally:
        observer.join(8)
    assert not observer.is_alive() and not failures
    assert 409 in paused_errors  # Real daemon rejection, not a mocked timeout.
    assert result[1:3] == (-1, True)
    assert 'FINAL' in result[0]
    assert log.read_bytes() == result[0].encode()
    assert execute(['printf', 'NEXT'], timeout=5, user=user)[:3] == ('NEXT', 0, False)


def test_never_resumed_pause_is_bounded_and_not_terminal(container, monkeypatch):
    monkeypatch.setattr(docker_utils, '_EXEC_PAUSE_RECOVERY_GRACE', .3)
    stop = threading.Event()
    terminal = []

    def freeze(chunk):
        if b'READY' in chunk:
            container.pause()
            stop.set()

    began = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='pause recovery expired'):
            docker_utils._exec_run(
                container, ['bash', '-c', 'echo READY; sleep 60'], timeout=5,
                shutdown_event=stop, on_chunk=freeze, on_terminal=terminal.append,
            )
        assert time.monotonic() - began < 3
        assert not terminal
        container.reload()
        assert container.attrs['State']['Paused']  # Never unpaused by the runner.
    finally:
        container.unpause()


@pytest.mark.parametrize('paused', [False, True])
def test_unrelated_conflict_is_preserved_without_pause_recovery(
    container, monkeypatch, paused
):
    stop = threading.Event()
    terminal = []
    response = requests.Response()
    response.status_code = 409
    original = docker.errors.APIError('conflict', response=response, explanation='unrelated conflict')
    control = container.exec_run
    attempts = []

    def conflict(command, **kwargs):
        if command[2] == ': > "$1/stop"':
            attempts.append(time.monotonic())
            raise original
        return control(command, **kwargs)

    def ready(chunk):
        if b'READY' in chunk:
            if paused:
                container.pause()
            stop.set()

    monkeypatch.setattr(container, 'exec_run', conflict)
    try:
        with pytest.raises(docker.errors.APIError) as raised:
            docker_utils._exec_run(
                container, ['bash', '-c', 'echo READY; sleep 60'], timeout=5,
                shutdown_event=stop, on_chunk=ready, on_terminal=terminal.append,
            )
        assert raised.value is original
        assert len(attempts) == 2  # Original call and existing best-effort cleanup.
        assert attempts[-1] - attempts[0] < .5
        assert not terminal
    finally:
        if paused:
            container.unpause()


def test_restarted_container_is_not_adopted_during_pause_recovery(container, monkeypatch):
    stop = threading.Event()
    terminal = []
    control = container.exec_run
    restarted = False

    def restart_before_stop(command, **kwargs):
        nonlocal restarted
        if command[2] == ': > "$1/stop"' and not restarted:
            restarted = True
            container.restart(timeout=0)
            container.pause()
        return control(command, **kwargs)

    monkeypatch.setattr(container, 'exec_run', restart_before_stop)
    try:
        with pytest.raises(RuntimeError, match='incarnation changed'):
            docker_utils._exec_run(
                container, ['bash', '-c', 'echo READY; sleep 60'], timeout=5,
                shutdown_event=stop,
                on_chunk=lambda chunk: stop.set() if b'READY' in chunk else None,
                on_terminal=terminal.append,
            )
        assert restarted and not terminal
    finally:
        container.unpause()


def test_normal_terminal_receipt_read_survives_snapshot_pause(container, execute, monkeypatch):
    control = container.exec_run
    resume = None
    pauses = []

    def pause_before_receipt(command, **kwargs):
        nonlocal resume
        if command[2] == 'cat -- "$1/settled"' and not pauses:
            container.pause()
            pauses.append(True)
            resume = threading.Timer(.3, container.unpause)
            resume.start()
        return control(command, **kwargs)

    monkeypatch.setattr(container, 'exec_run', pause_before_receipt)
    try:
        result = execute(['bash', '-c', 'echo DONE; exit 17'], timeout=5)
        assert pauses and result[:3] == ('DONE\n', 17, False)
    finally:
        if resume:
            resume.join(3)


def test_missing_container_does_not_produce_terminal_receipt(container, monkeypatch):
    stop = threading.Event()
    terminal = []
    control = container.exec_run
    removed = False

    def remove_before_stop(command, **kwargs):
        nonlocal removed
        if command[2] == ': > "$1/stop"' and not removed:
            removed = True
            container.remove(force=True)
        return control(command, **kwargs)

    monkeypatch.setattr(container, 'exec_run', remove_before_stop)
    with pytest.raises(docker.errors.NotFound):
        docker_utils._exec_run(
            container, ['bash', '-c', 'echo READY; sleep 60'], timeout=5,
            shutdown_event=stop,
            on_chunk=lambda chunk: stop.set() if b'READY' in chunk else None,
            on_terminal=terminal.append,
        )
    assert removed and not terminal


def test_pause_during_group_shutdown_does_not_consume_drain_grace(
    container, execute, monkeypatch, tmp_path
):
    monkeypatch.setattr(docker_utils, '_EXEC_SHUTDOWN_GRACE', 1)
    monkeypatch.setattr(docker_utils, '_EXEC_DRAIN_GRACE', 1)
    stop = threading.Event()
    resumes = []

    def on_chunk(chunk):
        if b'READY' in chunk:
            stop.set()
        if b'STOPPING' in chunk:
            container.pause()
            resume = threading.Timer(2.5, container.unpause)
            resumes.append(resume)
            resume.start()

    body = '''import signal,time,sys
def stop(*_):
    print('STOPPING',flush=True)
    time.sleep(.4)
    print('FINAL',flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM,stop)
print('READY',flush=True)
time.sleep(60)
'''
    log = tmp_path / 'drain.log'
    try:
        result = execute(
            ['python3', '-u', '-c', body], timeout=5,
            shutdown_event=stop, on_chunk=on_chunk, log_file=log,
        )
        assert resumes and result[1:3] == (-1, True)
        # The in-container grace is wall-clock based and may escalate on resume;
        # every emitted byte must still be drained, whether FINAL was emitted.
        assert 'STOPPING' in result[0]
        assert log.read_bytes() == result[0].encode()
    finally:
        for resume in resumes:
            resume.join(5)


def test_repeated_pause_rejections_share_one_recovery_deadline(container, monkeypatch):
    monkeypatch.setattr(docker_utils, '_EXEC_PAUSE_RECOVERY_GRACE', .2)
    stop = threading.Event()
    terminal = []
    response = requests.Response()
    response.status_code = 409
    conflict = docker.errors.APIError(
        'pause race', response=response,
        explanation=f'Container {container.id} is paused, unpause the container before exec',
    )
    control = container.exec_run
    attempts = []

    def reject_stop(command, **kwargs):
        if command[2] == ': > "$1/stop"':
            attempts.append(time.monotonic())
            # Model a snapshot that resumes just before each fresh inspect.
            raise conflict
        return control(command, **kwargs)

    monkeypatch.setattr(container, 'exec_run', reject_stop)
    with pytest.raises(RuntimeError, match='pause recovery expired'):
        docker_utils._exec_run(
            container, ['bash', '-c', 'echo READY; sleep 60'], timeout=5,
            shutdown_event=stop,
            on_chunk=lambda chunk: stop.set() if b'READY' in chunk else None,
            on_terminal=terminal.append,
        )
    assert len(attempts) > 2 and not terminal
    assert .2 <= attempts[-1] - attempts[0] < .7
