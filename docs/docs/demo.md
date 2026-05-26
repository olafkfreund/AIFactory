---
title: Demo
sidebar_position: 3
---

# Demo — Claude plans, the portal builds

The repo ships with `scripts/demo.sh` — a Bash script that walks the full flow against a fixed public sample repo (`dataseeek/aifactory-demo`):

1. Seed the demo repo with 3 GitHub issues
2. Register the repo with your local portal
3. Import the issues into the backlog as tasks
4. Show the portal picks them up automatically
5. Drive Claude Code from the terminal to refine a spec, watch the portal reflect it
6. Kick off an autonomous build via the portal, watch the agent run in the Live Console

## Prerequisites

- The portal running locally (see [Getting Started](./getting-started))
- `gh` CLI authenticated (`gh auth status`)
- `jq` installed (`brew install jq` / `apt-get install jq`)

## Run

```bash
./scripts/demo.sh
```

Each step prints a banner and waits for you to press Enter. Pass `--yolo` to run uninterrupted.

```bash
./scripts/demo.sh --yolo
```

## What you'll see

Each step is screenshotted below.

> Screenshots are added in Phase B2 — see the [showcase walkthrough](./showcase/demo-walkthrough) for the final visual tour.

## Tear-down

The script doesn't unregister the project from your local portal — close the demo task and remove the project from the Welcome screen if you don't want it lingering.
