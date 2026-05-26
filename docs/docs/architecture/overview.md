---
title: Overview
sidebar_position: 1
---

# Architecture Overview

AIFactory has three runtime components:

```mermaid
flowchart LR
    Browser[Browser<br/>React + xterm]
    WebServer[Web Server<br/>FastAPI + WebSocket]
    Backend[Agent Runtime<br/>Claude Agent SDK + run.py]
    Browser <-->|REST + WS| WebServer
    WebServer -->|spawn subprocess| Backend
    Backend -->|writes| Repo[(Git Repo<br/>.aifactory/specs/)]
    WebServer -->|reads| Repo
    Backend -.->|optional<br/>AIFACTORY_RMUX_ENABLED| rmux[rmux daemon]
    rmux -.->|FIFO bytes| WebServer
```

- **Frontend** (`apps/frontend-web/`) — React 19 + Vite + xterm. Talks REST + WebSocket to the web-server.
- **Web Server** (`apps/web-server/`) — FastAPI service. Handles auth, project/task CRUD, GitHub integration, audit logging. Spawns the agent runtime as a subprocess per task.
- **Agent Runtime** (`apps/backend/`) — The Python CLI (`run.py`, `spec_runner.py`) that drives the agent pipeline. Talks to LLM providers via the Claude Agent SDK or the provider abstraction.

## Where the code lives

```
apps/
├── frontend-web/   # React UI (browser, port 3100)
├── web-server/     # FastAPI (port 3101)
└── backend/        # CLI + agent runtime (subprocess)
```

## How a task moves through the system

```mermaid
sequenceDiagram
    actor User
    participant UI as Frontend (React)
    participant API as Web Server
    participant Disk as .aifactory/specs/
    participant Agent as Agent Runtime
    participant LLM

    User->>UI: Create task "Add /healthz"
    UI->>API: POST /api/projects/{id}/tasks
    API->>Disk: write spec.md, requirements.json
    API-->>UI: 200 + taskId
    User->>UI: Click "Start"
    UI->>API: POST /api/tasks/{id}/recover {autoRestart: true}
    API->>Agent: spawn run.py --spec NNN
    Agent->>LLM: planner phase (Claude)
    LLM-->>Agent: implementation_plan.json
    Agent->>Disk: write implementation_plan.json
    Agent->>LLM: coder phase (Ollama/Claude)
    LLM-->>Agent: code diffs
    Agent->>Disk: apply diffs in worktree
    Agent->>LLM: qa phase (Claude)
    LLM-->>Agent: qa_report.md
    API-->>UI: WS task:update events throughout
    UI->>User: Live Console + Kanban updates
```

## Security model

Three defense layers, applied at every agent run:

1. **OS sandbox** — bash commands are isolated; the agent process can't escape the project directory
2. **Filesystem permissions** — agents can only touch files under `project_path`
3. **Command allowlist** — dynamically generated from the detected project stack (see `apps/backend/core/security.py` and `project_analyzer.py`); cached in `.aifactory-security.json`

OAuth tokens never leak to subprocesses. The `ANTHROPIC_API_KEY` is scrubbed from the env passed to `run.py` (see commit `017eed3`); only the OAuth-issued token reaches Claude.

## Where to dig next

- **[Agents →](./agents)** — what each agent does and what prompts drive it
- **[Data Flow →](./data-flow)** — how worktrees, sessions, and audit logs interact
- **API Reference →** auto-generated from the FastAPI OpenAPI spec (Phase B2 follow-up)
