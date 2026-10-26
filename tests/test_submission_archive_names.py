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

"""Submission names survive filtering and GNU tar extraction byte for byte."""

from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from sforge.harness.backend.base import ExecResult, StreamingExecResult
from sforge.harness.config import SForgeConfig
from sforge.harness.run_evaluation import (
    EvaluationError,
    _filter_archive_files,
    judge_submission,
)
from sforge.harness.task_spec import JudgeSpec, TaskSpec, WorkSpec


NAMES = [
    "answer.txt", "记录.txt", "line\nbreak", "carriage\rreturn",
    "record\x1eseparator", "vertical\vtab", "form\ffeed", "tab\tname",
    "back\\slash", "literal\\nsequence", " leading", "trailing ",
    " padded ", "--checkpoint=1", "-C", "long-" + "x" * 160,
    "unicode\u2028line", "byte-\udcff",
]


def archive_bytes(files, *, format=tarfile.PAX_FORMAT, directories=()):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=format) as archive:
        for name in directories:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, content in files:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


class LocalTarBackend:
    """Exercise the real extraction commands in a disposable local workspace."""

    backend_name = "synthetic"

    def __init__(self, root):
        self.root = root
        self.root.mkdir()
        self.cleaned = False

    def create_container(self, *_args, **_kwargs):
        return SimpleNamespace(id="synthetic")

    def start_container(self, _handle):
        pass

    def copy_to_container(self, _handle, source, destination):
        shutil.copyfile(source, self.root / destination.name)

    def exec_run(self, _handle, command, *, workdir, **_kwargs):
        argv = shlex.split(command)
        argv = [str(self.root / Path(arg).name) if arg.startswith("/tmp/") else arg
                for arg in argv]
        result = subprocess.run(argv, cwd=workdir, capture_output=True, timeout=10)
        return ExecResult(result.stdout.decode(errors="replace") +
                          result.stderr.decode(errors="replace"), result.returncode)

    def exec_run_with_timeout(self, handle, command, _timeout, **kwargs):
        result = self.exec_run(handle, command, **kwargs)
        return StreamingExecResult(output=result.output, exit_code=result.exit_code)

    def cleanup_container(self, _handle, _logger=None):
        self.cleaned = True


def task_spec(cwd):
    return TaskSpec(
        task_id="names", name="Archive names", benchmark_name="synthetic",
        base_image="python", platform="linux/amd64", cwd=str(cwd),
        submit_paths=["."], submit_exclude=["tests", "*.secret"],
        work=WorkSpec("specs", "solve", image_tag="fixture"),
        judge=JudgeSpec("echo '{\"score\": 1, \"valid\": true}'", 10,
                        "structured_json", image_tag="fixture"),
    )


@pytest.mark.parametrize("format", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
@pytest.mark.parametrize("prefix", ["", "./"])
def test_judge_roundtrip_preserves_names(tmp_path, format, prefix):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = LocalTarBackend(tmp_path / "transport")
    expected = {name: f"payload-{i}".encode() for i, name in enumerate(NAMES)}
    files = [(prefix + name, content) for name, content in expected.items()]
    # A directory header must never cause excluded descendants to be extracted.
    files += [(prefix + "tests/hidden", b"excluded"),
              (prefix + "folder/excluded.secret", b"excluded")]
    expected["folder/answer"] = b"allowed"
    files += [(prefix + "folder/answer", b"allowed")]
    archive = archive_bytes(files, format=format, directories=[prefix + "folder"])
    report = judge_submission(task_spec(workspace), archive,
                              SForgeConfig(log_dir=tmp_path), backend,
                              log_dir=tmp_path / "result")
    actual = {str(p.relative_to(workspace)): p.read_bytes()
              for p in workspace.rglob("*") if p.is_file()}
    assert actual == expected
    assert report.valid and backend.cleaned


def test_filter_preserves_spelling_but_checks_canonical_paths():
    names = ["./src/ok", "src//also", "src/./third", "src/ padded ",
             "./src/tests/hidden", "src//tests/hidden", "src/x.secret",
             "outside/file", "src/../outside", "/src/absolute", "src/a\0b"]
    assert _filter_archive_files(names, ["./src/"], ["./src/tests/", "*.secret"]) == names[:4]


@pytest.mark.parametrize("name", ["../escape", "a/../../escape", "/absolute", "a\0b"])
def test_filter_rejects_unsafe_paths(name):
    assert _filter_archive_files([name], ["."], []) == []


def test_empty_whitelist_does_not_allow_everything():
    assert _filter_archive_files(["answer"], [""], []) == []


def test_invalid_archive_is_error_not_score(tmp_path):
    backend = LocalTarBackend(tmp_path / "transport")
    with pytest.raises(EvaluationError, match="judge_execution_failed:extract_submission"):
        judge_submission(task_spec(tmp_path), b"not a tar archive",
                         SForgeConfig(log_dir=tmp_path), backend,
                         log_dir=tmp_path / "result")
    assert backend.cleaned
    assert not (tmp_path / "result/report.json").exists()


def test_rejected_paths_do_not_extract_or_change_the_skeleton(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skeleton = workspace / "answer"
    skeleton.write_bytes(b"original")
    backend = LocalTarBackend(tmp_path / "transport")
    files = [("../escape", b"bad"), (str(tmp_path / "absolute"), b"bad"),
             ("./tests/hidden", b"bad"), ("folder/../answer", b"bad")]
    judge_submission(task_spec(workspace), archive_bytes(files),
                     SForgeConfig(log_dir=tmp_path), backend,
                     log_dir=tmp_path / "result")
    assert list(workspace.iterdir()) == [skeleton]
    assert skeleton.read_bytes() == b"original"
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "absolute").exists()


def test_duplicate_members_and_relative_links_keep_tar_semantics(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = LocalTarBackend(tmp_path / "transport")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for content in (b"first", b"last"):
            member = tarfile.TarInfo("answer")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        for name, kind in [("sym", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE)]:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "answer"
            archive.addfile(member)
    judge_submission(task_spec(workspace), raw.getvalue(),
                     SForgeConfig(log_dir=tmp_path), backend,
                     log_dir=tmp_path / "result")
    assert (workspace / "answer").read_bytes() == b"last"
    assert (workspace / "sym").is_symlink()
    assert (workspace / "sym").read_bytes() == b"last"
    assert (workspace / "hard").stat().st_ino == (workspace / "answer").stat().st_ino


def test_real_docker_archive_roundtrip(tmp_path, monkeypatch):
    """Opt-in qualification against the real Docker transport, without a build."""
    image = os.environ.get("SFORGE_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("set SFORGE_DOCKER_TEST_IMAGE to a cached image with GNU tar")
    import docker
    from sforge.harness import run_evaluation
    from sforge.harness.backend.docker_backend import DockerBackend

    client = docker.from_env()
    client.images.get(image)  # Never pull or modify benchmark images.
    collection_type = type(client.containers)
    create = collection_type.create

    def isolated_create(collection, *args, **kwargs):
        return create(collection, *args, network_disabled=True, **kwargs)

    monkeypatch.setattr(collection_type, "create", isolated_create)
    handles = []
    expected = {name: f"payload-{i}" for i, name in enumerate(NAMES)}

    class TestBackend(DockerBackend):
        def create_container(self, _image, name, **kwargs):
            handle = super().create_container(image, name, **kwargs)
            handles.append(handle)
            return handle

        def start_container(self, handle):
            super().start_container(handle)
            result = self.exec_run(handle, "mkdir -m 777 /tmp/answer", user="root")
            assert result.exit_code == 0

    monkeypatch.setattr(run_evaluation, "build_judge_image", lambda *_args, **_kwargs: None)
    spec = task_spec("/tmp/answer")
    script = (
        "import json,pathlib; "
        "actual={str(p.relative_to('.')):p.read_text() "
        "for p in pathlib.Path('.').rglob('*') if p.is_file()}; "
        f"assert actual == {expected!r}, repr(actual); "
        "print(json.dumps({'score':1, 'valid':True}))"
    )
    spec.judge.eval_cmd = "python -c " + shlex.quote(script)
    files = [(name, content.encode()) for name, content in expected.items()]
    files += [("./tests/hidden", b"excluded")]
    try:
        report = judge_submission(spec, archive_bytes(files),
                                  SForgeConfig(log_dir=tmp_path, judge_cpu_limit=1,
                                               judge_mem_limit="128m"),
                                  TestBackend(client), submission_id=uuid.uuid4().hex,
                                  log_dir=tmp_path / "result")
        assert report.valid and report.score == 1
        assert handles and not client.containers.list(all=True, filters={"id": handles[0].id})
    finally:
        for handle in handles:
            try:
                handle.raw.remove(force=True)
            except docker.errors.NotFound:
                pass
        client.close()
