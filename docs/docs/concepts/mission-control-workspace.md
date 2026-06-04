---
title: Mission Control workspace
sidebar_position: 4
---

# Mission Control workspace

> One screen to run a task: the plan on the left, the agent's live activity in the middle, and the result — a running preview, the diff, or the merge controls — on the right.

Mission Control is a full-page, three-pane task workspace. The compact task-detail modal is still there for a quick look; Mission Control is for when you want to *drive* a task and watch it work without losing context to tab-switching.

## Opening it

Open any task to bring up the task-detail view, then click the **⤢ (expand)** button in the header. Mission Control takes over the screen.

- **Esc** closes it.
- The **⧉ (collapse)** button drops you back into the compact modal for the same task.
- Pane widths are draggable and remembered between sessions.

## The three panes

```
┌──────────────────────────────────────────────────────────────────┐
│  SPEC-5  Add user auth      ● LIVE   ▓▓▓▓▓░░ 62%        [ Stop ]   │
├───────────────┬────────────────────────────┬─────────────────────┤
│ PLAN &        │  ACTIVITY                  │  OUTPUT             │
│ SUBTASKS      │  ┌ Activity ┬ Live Console │  ┌ Preview ┬ Files  │
│               │  │ ▸ planning ✓            │  │ ┬ Review          │
│  ▸ 1 ✓        │  │ ▸ coding 3/7 ●          │  │                   │
│  ▸ 2 ✓        │  │   › edit auth.py +42    │  │  live app / diff  │
│  ▸ 3 ● 60%    │  │   › run pytest ✓        │  │  / merge controls │
│  ▸ 4 …        │  │ [ embedded terminal ]   │  │                   │
├───────────────┴────────────────────────────┴─────────────────────┤
│  ⏸ Plan ready — approve to start coding      [ Reject ] [ Approve ]│
└──────────────────────────────────────────────────────────────────┘
```

### 1. Plan & Subtasks (left)

The work breakdown and live progress:

- A **phase timeline** (Plan → Review → Code → QA → Done) that fills as the run advances, with a live elapsed timer and a pulsing **LIVE** indicator while the agent is active.
- The **plan-review** controls when a task is paused waiting for you to approve its plan.
- The **subtask checklist** with per-subtask status, plus task metadata.

### 2. Activity (center)

What the agent is doing right now:

- **Activity** — the phase-grouped log stream (planning / coding / validation), with tool calls, edits, and errors animating in as they arrive.
- **Live Console** — the [rmux Live Agent Console](./rmux-live-console) embedded directly in the workspace, so you can watch the agent's terminal (and Attach to drive it) without leaving the task. This tab appears when the Live Console is enabled on the server.

### 3. Output (right)

The result of the build, as tabs:

- **Preview** — a running-app preview in an iframe. Point it at your dev server (an address bar plus one-click port presets for 3000 / 5173 / 8080 / …) to see the change rendered while the agent iterates.
- **Files** — the worktree file browser / diff for the task's branch.
- **Review** — the full human-review flow when a task is awaiting you: inspect the diff, **stage**, **merge**, **create a PR**, or **reject** with feedback. A small dot on the tab flags when review is pending.

## Portal polish

Mission Control ships alongside a round of portal UX upgrades that apply across the whole web UI:

- **Skeleton loaders** on the Kanban board and logs instead of bare spinners, so loading reads as fast.
- **Live progress** — the task progress bar gained a real elapsed timer, an animated "working" gradient, and a labeled phase timeline.
- **Theme-aware terminal** — the embedded terminal now follows the active theme (light / dark, Gruvbox / shadcn) instead of being hardcoded dark.
- **"Waiting for you" beacon** — tasks parked in human review get an unmissable glow + banner on their card, so an approval gate never sits unnoticed.
- **Streaming log entries** animate in as the agent emits them, giving the activity feed a live pulse.

## Status

Shipping in **PR #311**. The compact task-detail modal remains the default; Mission Control is opened on demand from the task header. The **Live Console** tab inside it follows the same [`APP_RMUX_ENABLED`](./rmux-live-console#enabling-it) server setting as the standalone console.
