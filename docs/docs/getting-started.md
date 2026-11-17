---
title: Getting Started
sidebar_position: 2
---

# Getting Started

This guide gets you from a fresh clone to a running portal in about 60 seconds.

## Prerequisites

- **Node.js 24+** (we use modern ESM features)
- **Python 3.12+** (the backend uses match statements + extended type syntax)
- **Claude Code** installed, with a token from `claude setup-token`

## Install

```bash
git clone https://github.com/olafkfreund/AIFactory
cd AIFactory
npm run install:all
```

`install:all` runs the backend Python deps via `uv` and the frontend npm deps. First run takes ~2 minutes.

## Configure your OAuth token

```bash
claude setup-token
# Copy the printed token, then:
echo "CLAUDE_CODE_OAUTH_TOKEN=<paste>" >> apps/backend/.env
```

## Start the portal

```bash
# Terminal 1 — backend (FastAPI + WebSocket)
cd apps/web-server
python -m server.main

# Terminal 2 — frontend (Vite dev server)
cd apps/frontend-web
npm run dev
```

Open **http://localhost:3100** — you should see the welcome screen. Add your first project and create a task.

## Connect GitHub (optional)

Out of the box AIFactory pushes and opens PRs with a maintainer's token. To make
the PR author a bot, so a maintainer can approve and merge it, register a GitHub
App: [GitHub App identity: operator setup](./concepts/github-app). It covers
creating and installing the App, loading its key and checking that the first PR
is authored by the App. Copilot reviews are opt-in; the same page explains how.

## What's next

- **[Run the demo →](./demo)** to see the full end-to-end flow against a public sample repo
- **[Concepts: Spec-Driven Development →](./concepts/spec-driven-development)** for the why behind the planner/coder/QA loop
- **[Architecture →](./architecture/overview)** for how the pieces fit together
