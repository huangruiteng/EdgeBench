# 简介

## 什么是 SForge

SForge 是一个 Code Agent 评测框架，专为超长程迭代式任务设计。Agent（如 Claude Code 和 Codex）在隔离的 Docker 容器中完成骨架代码，然后提交到独立的 Judge 容器进行评分。Agent 可以多次提交，根据测试反馈迭代改进代码，直到超时为止，最终取所有提交中的最优分作为最终成绩。

SForge 原生支持 EdgeBench，包含 **130+ 评测任务**，任务覆盖 Python、Java、Go、Rust、C/C++ 等多类运行环境，支持测试驱动、分数优化、交互式游戏、定理证明等多种评测类型。

## 核心机制

SForge 的设计围绕三个核心机制：

1. **双容器隔离** — Work 和 Judge 环境完全分离，从根本上防止评测作弊。Agent 永远无法访问测试套件。
2. **闭环迭代评测** — Agent 并非在时间结束后一次性提交并得分，而是在整个运行过程中反复提交，获得细粒度反馈（通过率、失败用例名、分数），据此改进后再次提交，直到超时为止——取所有提交的最优分作为最终成绩。
3. **长时程执行保障** — Stop hook 阻止 Agent 过早退出，auto-resume 从瞬时故障中恢复，Kubernetes 后端支持大规模并行运行。

## 双容器架构

SForge 的核心设计是将 Agent 工作环境与评测环境完全隔离：

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

每个任务构建三个 Docker 镜像：

| 镜像 | 内容 | Agent 可见？ |
|------|------|-------------|
| `<benchmark>.base.<base_image>:<hash>` | 语言运行时 + 常用工具 | — |
| `<benchmark>.work.<task>:<hash>` | 骨架代码 + 文档，无测试 | 是 |
| `<benchmark>.judge.<task>:<hash>` | 骨架代码 + 完整测试套件 | 否 |

- **Work 镜像**：Agent 的工作区。包含骨架代码和任务文档，但测试脚本已被删除。Agent 在这里编写代码，通过 `sforge-submit` 提交评测。
- **Judge 镜像**：评测环境。包含完整的测试套件和评测脚本。Agent 永远无法访问这些内容。Judge Server 在收到提交后，会启动一个临时的 Judge 容器来运行测试。
- **Base 镜像**：每种语言共享一个基础镜像（如 `python:3.11`、`golang:1.22`），预装了语言运行时和常用工具。

## 迭代式评测

SForge 支持 Agent 多次提交代码进行评测，每次都能获得反馈：

1. Agent 在 Work 容器中实现代码
2. 调用 `sforge-submit` 提交当前代码
3. Judge Server 启动 Judge 容器，运行测试，返回反馈（通过率、失败的测试名等）
4. Agent 根据反馈改进代码，再次提交
5. 重复上述过程直到超时，取所有提交的最高分

此外，SForge 内置了 **auto-eval 自动评测**机制：后台守护进程会定期（默认 300 秒）自动提交代码进行评测，确保 Agent 的进展被及时记录，即使 Agent 忘记手动提交。

## 长时程执行保障

评测任务通常需要数十分钟甚至数小时运行，SForge 通过多种机制确保 Agent 能充分利用分配的时间：

- **Stop Hook**：拦截 Agent 的退出请求，阻止其在时间耗尽前提前退出。Agent 认为"任务已完成"时，Stop Hook 会要求它继续检查和改进代码。
- **Auto-Resume**：当 Agent 因 API 断连、瞬时错误等异常退出时，框架自动使用 Agent 的会话恢复机制（如 `claude --continue`）重新启动，并将剩余超时预算传递给恢复后的会话。
  首次启动和恢复后的 Agent 进程都将 core dump 软限制设为零。需要调试时，工具可以在继承的硬限制内提高自己的软限制，并将转储写到提交范围以外。
- **Kubernetes 后端**：支持大规模并行运行，同时评测多个任务或多个 Agent。
