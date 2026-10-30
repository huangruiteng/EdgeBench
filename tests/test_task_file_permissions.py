"""Generated task inputs must survive restrictive host permissions and UIDs."""

from pathlib import PurePosixPath
from types import SimpleNamespace
import os

import pytest

from sforge.harness.run_agent import _install_task_file, _install_tools


@pytest.mark.parametrize("failing_call", [0, 1, 2])
def test_install_failure_never_continues(failing_call, tmp_path):
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(exit_code=1 if len(calls) - 1 == failing_call else 0)

    backend = SimpleNamespace(copy_to_container=lambda *args: None, exec_run=execute)
    with pytest.raises(RuntimeError, match="(permissions|unavailable)"):
        _install_task_file(backend, object(), tmp_path / "input",
                           PurePosixPath("/tmp/task-script"), executable=True)
    assert len(calls) == failing_call + 1
    assert all(call[1].get("user") is None for call in calls[1:])


def test_generated_files_readable_across_uids_in_real_docker(tmp_path):
    """Exercise transfer and script interpretation with the real container user."""
    import docker
    from sforge.harness.backend.docker_backend import DockerBackend, DockerContainerHandle

    image = os.environ.get("SFORGE_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("set SFORGE_DOCKER_TEST_IMAGE to a cached bash/python3 image")
    client = docker.from_env()
    client.images.get(image)
    container = client.containers.run(image, ["sleep", "120"], detach=True,
                                    network_disabled=True)
    try:
        # Deliberately differ from the host UID preserved by tar transfer.
        uid = os.getuid() + 10000
        assert container.exec_run(["useradd", "-u", str(uid), "agent"]).exit_code == 0
        backend, handle = DockerBackend(client), DockerContainerHandle(container)
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Complete the actual task.\n", encoding="utf-8")
        prompt.chmod(0o600)
        script = tmp_path / "submit.sh"
        script.write_text("#!/bin/sh\nprintf 'submission-ready\\n'\n", encoding="utf-8")
        script.chmod(0o600)
        for source, destination, executable in [
            (prompt, "/tmp/task-prompt.md", False),
            (script, "/usr/local/bin/task-submit", True),
        ]:
            target = PurePosixPath(destination)
            backend.copy_to_container(handle, source, target)
            assert backend.exec_run(handle, ["cat", destination], user="agent").exit_code != 0
            _install_task_file(backend, handle, source, target, executable=executable)
            assert source.stat().st_mode & 0o777 == 0o600
        observed = backend.exec_run(handle, ["cat", "/tmp/task-prompt.md"], user="agent")
        assert observed.exit_code == 0 and observed.output == prompt.read_text()
        submitted = backend.exec_run(handle, ["/usr/local/bin/task-submit"], user="agent")
        assert submitted.exit_code == 0 and submitted.output == "submission-ready\n"
        # Also exercise the actual runner installation and generated Bash script.
        previous_mask = os.umask(0o077)
        try:
            _install_tools(backend, handle, None, None, tmp_path,
                           SimpleNamespace(info=lambda *args: None), disable_stop_hook=True)
        finally:
            os.umask(previous_mask)
        help_result = backend.exec_run(handle, ["/usr/local/bin/sforge-submit", "--help"], user="agent")
        assert help_result.exit_code == 0
        assert "Usage: sforge-submit" in help_result.output
        assert (tmp_path / "_sforge-submit.sh").stat().st_mode & 0o777 == 0o600
    finally:
        container.remove(force=True)
        client.close()
