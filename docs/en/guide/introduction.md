# Introduction

## What is SForge

SForge is a Code Agent evaluation framework designed for ultra-long-horizon iterative tasks. Agents such as Claude Code and Codex complete skeleton code inside isolated Docker containers, then submit to an independent Judge container for scoring. Agents can submit multiple times, iterate based on test feedback until timeout, and the best score across all submissions is used as the final result.

SForge natively supports EdgeBench, which contains **130+ evaluation tasks**. Tasks cover Python, Java, Go, Rust, C/C++, and other runtime environments, and support multiple evaluation types such as test-driven tasks, score optimization, interactive games, and theorem proving.

## Key Mechanisms

SForge's design centers on three mechanisms:

1. **Two-container isolation** — work and judge environments are fully separated, preventing evaluation hacking at its root. The agent never sees the test suite.
2. **Iterative evaluation with feedback** — agents don't submit once at the end for a one-shot score; instead they submit throughout the run, receive granular feedback (pass rates, failing tests, scores), and improve in a closed loop until timeout — the best result across all submissions is the final score.
3. **Long-horizon execution** — stop hooks prevent premature agent exit, auto-resume recovers from transient failures, and the Kubernetes backend enables parallel runs at scale.

## Two-Container Architecture

SForge's core design fully isolates the agent workspace from the evaluation environment:

```
┌─────────────────────────┐           ┌───────────────────────────────┐
│     Work Container      │           │     Judge Server (host)       │
│                         │  archive  │                               │
│  skeleton code + docs   │ ────────> │  POST /api/v1/submit          │
│  NO test scripts        │  (HTTP)   │    → spawn judge container    │
│                         │           │    → extract archive          │
│  agent works here       │ <──────── │    → run tests                │
│  sforge-submit          │  results  │    → return score + details   │
└─────────────────────────┘           └───────────────────────────────┘
```

Each task builds three Docker images:

| Image | Contents | Agent can see? |
|-------|----------|----------------|
| `<benchmark>.base.<base_image>:<hash>` | Language runtime + common tools | — |
| `<benchmark>.work.<task>:<hash>` | Skeleton code + docs, no tests | Yes |
| `<benchmark>.judge.<task>:<hash>` | Skeleton code + full test suite | No |

- **Work image**: the agent workspace. It contains skeleton code and task docs, but test scripts have been removed. The agent writes code here and submits for evaluation via `sforge-submit`.
- **Judge image**: the evaluation environment. It contains the full test suite and evaluation scripts. The agent can never access these contents. When the Judge Server receives a submission, it starts an ephemeral Judge container to run tests.
- **Base image**: a shared base image per language, such as `python:3.11` or `golang:1.22`, with the language runtime and common tools preinstalled.

## Iterative Evaluation

SForge lets agents submit code for evaluation multiple times and receive feedback each time:

1. The agent implements code in the Work container
2. The agent calls `sforge-submit` to submit the current code
3. The Judge Server starts a Judge container, runs tests, and returns feedback such as pass rate and failing test names
4. The agent improves the code based on feedback and submits again
5. This repeats until timeout; the highest score across all submissions is used

In addition, SForge includes an **auto-eval** mechanism: a background daemon periodically submits the code for evaluation (default: every 300 seconds), ensuring that agent progress is recorded in time even if the agent forgets to submit manually.

## Long-Horizon Execution

Evaluation tasks typically run for tens of minutes or even hours. SForge uses several mechanisms to ensure agents fully utilize the allocated time:

- **Stop Hook**: intercepts the agent's exit requests and prevents it from quitting before time runs out. When the agent decides the "task is done," the stop hook tells it to keep checking and improving.
- **Auto-Resume**: when the agent crashes due to API disconnects, transient errors, or other abnormal exits, the harness automatically re-launches it using the agent's native session resume mechanism (e.g., `claude --continue`) and passes the remaining timeout budget to the resumed session.
  Both initial and resumed agent shells start with a soft core-dump limit of zero. For debugging, a tool can raise its own soft limit up to the inherited hard limit and write its dump outside the submission paths.
- **Kubernetes backend**: enables large-scale parallel runs, evaluating multiple tasks or agents simultaneously.
