# Contributing to SForge

Thank you for your interest in contributing to SForge! This document covers the guidelines and workflow for contributing to the harness codebase.

## Prerequisites

- Python 3.10+
- Docker Engine 24+
- `pip` for local editable installs

## Getting Started

```bash
# Clone the repository
git clone <repo-url> && cd SForge

# Install locally for development
pip install -e .

# Fetch task definitions and verify the CLI
sforge fetch-tasks edgebench
sforge list
```

## Project Structure

```
sforge/
├── cli.py                  # CLI entry point (build/run/eval/serve/list)
├── harness/
│   ├── config.py           # SForgeConfig — environment variable injection
│   ├── constants.py        # Base image registry, log markers
│   ├── task_spec.py        # TaskSpec dataclass (WorkSpec + JudgeSpec)
│   ├── agent/              # Pluggable agent abstraction
│   ├── backend/            # Container backends (Docker, K8s)
│   ├── docker_build.py     # Image building
│   ├── run_agent.py        # Agent execution orchestration
│   ├── run_evaluation.py   # Judge submission grading
│   ├── judge_server.py     # FastAPI REST API
│   ├── evolve_scripts.py   # In-container script generators
│   ├── grading.py          # EvalReport and scoring
│   └── log_parsers/        # Per-format test output parsers
└── visualizer/             # Web UI for browsing run results
```

## Development Workflow

1. Create a feature branch from `main`.
2. Make your changes — keep commits focused and atomic.
3. Run the checks described below before opening a PR.
4. Open a pull request with a clear description of *what* and *why*.

## Code Style

The project follows standard Python conventions. No automated formatter is enforced yet, but please adhere to the existing style:

- **Type annotations** — use modern syntax (`str | None`, `dict[str, str]`). All modules should include `from __future__ import annotations`.
- **Dataclasses** — prefer `@dataclass` for structured data over plain dicts or tuples.
- **Naming** — `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants. Prefix private helpers with `_`.
- **Docstrings** — module-level docstrings are encouraged. Keep function/class docstrings short and only when the name is not self-explanatory.
- **Comments** — explain *why*, not *what*. Avoid obvious or redundant comments.

## License Headers

All `.py` source files must include the Apache 2.0 license header at the top:

```python
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
```

For files with a shebang (`#!/usr/bin/env python3`), the license header goes immediately after the shebang line.

## Running Tests

```bash
python -m pytest tests/ -v
```

## Common Contribution Scenarios

### Adding a New Agent

1. Create `sforge/harness/agent/<agent_name>.py` — subclass `Agent` from `sforge.harness.agent.base`.
2. Register the agent in `sforge/harness/agent/factory.py`.
3. Define `install_cmds` (how to install the agent CLI in a container) and implement the `run()` method.

### Adding a New Task

1. Create `tasks/<task_id>.json` with `work` and `judge` sections.
2. Work `setup_cmds` must delete test files and run `git init`; Judge keeps the full test suite.
3. If the language requires a new base image, add it to `BASE_IMAGE_REGISTRY` in `constants.py`.
4. If the test output format is new, add a parser (see below).

### Adding a Log Parser

1. Create `sforge/harness/log_parsers/<format>.py`.
2. Implement a `parse_<format>(log: str) -> list[TestResult]` function.
3. Register it in `sforge/harness/log_parsers/__init__.py`.
4. Reference the parser name in your task JSON's `judge.parser` field.

### Modifying the Judge Server API

The judge server (`sforge/harness/judge_server.py`) is a FastAPI app. When changing endpoints:

- Keep the `/api/v1/` prefix for all routes.
- Submissions are async — `POST /submit` enqueues, `GET /result/{id}` polls.
- Ensure backward compatibility or bump the API version.

## Pull Request Guidelines

- Keep PRs focused — one logical change per PR.
- Include a summary of what changed and why in the PR description.
- If your change affects task definitions or the judge API, describe how you tested it end-to-end (e.g., `sforge build` + `sforge serve` + `sforge run`).
- Do not commit files that contain secrets, API keys, or credentials.

## Reporting Issues

When filing a bug report, please include:

- The command you ran and its full output.
- Your Python version (`python --version`) and OS.
- Docker version (`docker --version`).
- The task ID and agent involved, if applicable.


## Shared contributor integration

The shared candidate for contributions from `huangruiteng` and `loopx-agent` is
[`huangruiteng/EdgeBench:codex/edgebench-integration`](https://github.com/huangruiteng/EdgeBench/tree/codex/edgebench-integration).
Both accounts should target this branch for composed validation. Upstream PRs
retain their independent review and merge status.

[The source manifest](docs/shared-integration.json) records every integrated PR
head. Update the manifest when a source changes, preserve ancestry, resolve
behavioral conflicts, and validate the resulting tree before advancing this
branch. Do not maintain separate competing integration branches per contributor.

Experiments must record an immutable integration commit and the effective task,
scoring, feedback and budget configuration. Optional checkpoint and Portfolio
scoring features being present in the tree does not mean they are enabled.
Historical attempts keep their original source and results when the candidate
advances; regrading or rerunning creates a distinct record.

The initial composition preserves the disk-spooled process/run admission model,
adds joint FIFO host slot/memory admission and confirmed Docker cleanup, retains
checkpoint identities and original deadlines, combines submission availability
with asynchronous result retrieval, and preserves explicit execution budgets.
Validation: 413 tests passed and four skipped with a cached Docker fixture.
