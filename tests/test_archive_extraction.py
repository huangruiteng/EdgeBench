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

"""Real archive transport through an isolated judge; no model or benchmark task."""

from __future__ import annotations

import io
import os
import tarfile
import uuid

import docker
import pytest

from sforge.harness.backend.docker_backend import DockerBackend
from sforge.harness.config import SForgeConfig
from sforge.harness.run_evaluation import EvaluationError, judge_submission
from sforge.harness.task_spec import JudgeSpec, TaskSpec, WorkSpec


@pytest.fixture
def judge(tmp_path, monkeypatch):
    image = os.environ.get("SFORGE_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("set SFORGE_DOCKER_TEST_IMAGE to a cached GNU tar/bash/python3 image")
    client = docker.from_env()
    base = client.images.get(image)  # Never pull or use a live benchmark image.
    name = "archive-test-" + uuid.uuid4().hex[:12]
    tag = f"{name}.judge.fixture:fixture"
    base.tag(tag)
    created = []
    collection_type = type(client.containers)
    create = collection_type.create

    def isolated_create(collection, *args, **kwargs):
        container = create(collection, *args, network_disabled=True, **kwargs)
        created.append(container)
        return container

    monkeypatch.setattr(collection_type, "create", isolated_create)
    spec = TaskSpec(
        task_id="fixture", name="archive transport", base_image="synthetic",
        platform="linux/amd64", cwd="/tmp", submit_paths=["src", "--checkpoint=1"],
        submit_exclude=["*.secret", "src/excluded"],
        work=WorkSpec(specs_dir="", agent_query="unused"),
        judge=JudgeSpec(eval_cmd="", eval_timeout=15, parser="structured_json",
                        image_tag="fixture"),
        benchmark_name=name,
    )
    config = SForgeConfig(log_dir=tmp_path / "logs", judge_cpu_limit=1,
                          judge_mem_limit="256m")
    try:
        yield spec, config, DockerBackend(client), created
    finally:
        leaked = client.containers.list(all=True, filters={"name": name})
        for container in leaked:
            container.remove(force=True)
        client.images.remove(tag)
        client.close()
        assert not leaked, "judge_submission must clean up success and failure paths"


def archive_of(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, body in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return raw.getvalue()


@pytest.mark.parametrize("names", [
    ["src/answer.py"],
    [r"src/report\n[one].json", r"src/tab\t.txt", r"src/octal\141.txt",
     "src/中文 [x].txt", "src/a b.txt", "--checkpoint=1"],
    [],
], ids=["ordinary", "literal", "all_filtered"])
def test_judge_extracts_exact_allowed_files(judge, names):
    spec, config, backend, created = judge
    expected = {name: f"content-{i}".encode() for i, name in enumerate(names)}
    rejected = {"outside.txt": b"no", "src/token.secret": b"no",
                "src/excluded/hidden.txt": b"no"}
    files = {**expected, **rejected}
    # The oracle checks actual extracted paths and bytes, independently of the
    # harness allowlist and command construction. No re-evaluation of real work.
    spec.judge.eval_cmd = "python3 - <<'PY'\n" + f"""
import json
from pathlib import Path
expected = {expected!r}
rejected = {list(rejected)!r}
for name, body in expected.items():
    assert Path(name).read_bytes() == body, repr(name)
for name in rejected:
    assert not Path(name).exists(), repr(name)
print(json.dumps({{'valid': True, 'score': len(expected)}}))
PY
"""
    report = judge_submission(spec, archive_of(files), config, backend)
    assert report.valid
    assert report.score == len(expected)
    assert len(created) == 1
    assert not backend.client.containers.list(all=True, filters={"id": created[0].id})


def test_corrupt_archive_remains_an_infrastructure_error(judge):
    spec, config, backend, created = judge
    with pytest.raises(EvaluationError, match="^judge_execution_failed:extract_submission$"):
        judge_submission(spec, b"not a tar archive", config, backend)
    assert len(created) == 1
    assert not list(config.log_dir.rglob("test_output.txt"))
    assert not list(config.log_dir.rglob("report.json"))
