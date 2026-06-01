# Split-screen live demo: developer ⇄ AIFactory

A two-screen, real-time demo of the full hand-off loop:

> A developer writes a task, hands it to AIFactory with `/handover`, AIFactory
> plans → pauses for review → codes → QA's → pauses for review, and the
> developer reviews the diff and merges. Both screens follow every step **live**.

```
┌──────────────────────────┬──────────────────────────┐
│  ① CLAUDE CODE (real CLI) │  ② AIFACTORY live feed    │   ③ PORTAL web UI
│     you run /handover     │     (right tmux pane)     │      (your browser)
└──────────────────────────┴──────────────────────────┘
        one tmux session, tiled next to the browser
```

- **① Left (tmux pane)** — a real Claude Code session in the developer's repo. This is where you type `/handover …`.
- **② Right (tmux pane)** — `scripts/demo_progress_feed.py`, a live, colourised feed of the build moving through `spec → plan → [REVIEW] → code → qa → [REVIEW] → done`. It calls out the two **"handed back to you"** review gates.
- **③ Browser** — the AIFactory portal (`http://localhost:3100`), the rich UI: kanban board, plan, logs, subtasks, and the diff + **Merge** button.

The feed (②) and the portal (③) show the *same* task from two angles — a terminal-native view for the presenter and the full product UI for the audience.

---

## Prerequisites

| Need | Check / fix |
|------|-------------|
| Portal backend (`:3101`) running | `cd apps/web-server && python -m server.main` |
| Frontend (`:3100`) running | `cd apps/frontend-web && npm run dev` |
| `~/.aifactory/.token` exists | created automatically on first portal boot |
| `tmux`, `claude`, `jq`, `curl` on PATH | the launcher fails fast and tells you what's missing |
| Backend venv | `npm run install:backend` (creates `apps/backend/.venv`) |
| `gh` authenticated | only needed the first time, to clone `olafkfreund/aifactory-demo` |

---

## Run it

```bash
# from the AIFactory repo root
./scripts/demo-split-screen.sh
```

What the launcher does:

1. **Preflights** the portal, token, and tooling.
2. **Provisions** `/tmp/aifactory-demo`:
   - clones (or pulls) the public demo repo,
   - registers it with the portal,
   - writes a `.mcp.json` + wrapper so the demo repo's Claude Code session has the `aifactory` MCP tools,
   - installs the `/handover` skill into the demo repo.
3. **Launches** a tmux session `aifactory-demo` split left/right (Claude Code | live feed).
4. **Opens** the portal in your browser.
5. **Attaches** you to the tmux session.

Tile the terminal window and the browser side-by-side for the full two-screen effect.

### Useful flags

```bash
./scripts/demo-split-screen.sh --no-browser        # don't auto-open the browser
./scripts/demo-split-screen.sh --no-attach         # create session, print attach cmd
./scripts/demo-split-screen.sh --no-provision      # reuse an already-wired demo repo
./scripts/demo-split-screen.sh --portal=http://host:3101 --frontend=http://host:3100
```

Run the right-pane feed on its own (e.g. on a second monitor or against a remote portal):

```bash
apps/backend/.venv/bin/python scripts/demo_progress_feed.py --portal http://localhost:3101
# lock onto a specific task instead of auto-discovering:
apps/backend/.venv/bin/python scripts/demo_progress_feed.py --task aifactory-demo:004-add-metrics-endpoint
```

---

## The run, beat by beat (presenter talk track)

Read this top-to-bottom while it plays. The user story maps 1:1 onto the beats.

### Beat 0 — "Here's the developer, working in their repo."
Both panes are up; the right feed says **● waiting for a handover…** and shows the exact command to type. *(Optional: make a small edit in the left repo first — e.g. drop a `# TODO: /metrics` stub — to literally show "the developer was writing code here".)*

### Beat 1 — "They hand the task to AIFactory." → `/handover`
In the **left** pane, type:

```
/handover Add a /metrics endpoint returning request counts, with a test
```

Claude will ask to approve the `aifactory` MCP tools the first time — approve them (this *is* the explicit hand-off). The skill registers/looks up the project and calls `task_create_and_run`.

> Talking point: *"The developer stays in their editor. One command moves the work into an autonomous pipeline — no context-switch into a separate tool."*

### Beat 2 — "AIFactory picks it up and starts planning."
The **right feed** locks onto the new task and lights `spec → plan`. The **portal** shows a new card moving into *In Progress*. The planner writes `spec.md` and `implementation_plan.json`.

### Beat 3 — ⏸ **PLAN-REVIEW GATE** — "It hands control back to me."
Both screens show **⏸ HANDED BACK TO YOU — PLAN READY FOR REVIEW**. This is the first hand-back. In the **portal**, open the task, read the plan, and click **Approve**.

> Talking point: *"AIFactory doesn't run off and write code blind. It pauses at a human gate so the developer signs off on the plan first."*

### Beat 4 — "It builds, and I watch it happen."
After approval the coder runs. In the **right feed**, subtasks tick from `○ → ◐ → ✓` and the progress bar fills; the activity tail streams file writes and test runs. In the **portal**, the *Subtasks*, *Logs*, and *Live Console* tabs show the same thing richer. QA then validates the acceptance criteria.

### Beat 5 — ✅ **FINAL-REVIEW GATE** — "It hands the finished work back for review."
Both screens show **✅ HANDED BACK TO YOU — BUILD COMPLETE**. In the **portal**, open the task → **Review** tab → inspect the diff. When you're happy, click **Merge** — the agent's commits land on your local branch.

> Talking point: *"The loop closes where it started: with the developer. They review a real diff and merge on their terms. AIFactory did the work; the human kept control at both ends."*

---

## What's happening under the hood

- `/handover` → `mcp__aifactory__task_create_and_run` → planner → coder → QA, all in an isolated git worktree (`.aifactory/worktrees/tasks/<spec>/`).
- The portal syncs worktree artifacts (`implementation_plan.json`, `build-progress.txt`, `qa_report.md`, …) to the main spec dir every ~3 s, which is what drives the live UI and the feed.
- The feed polls REST (`/api/tasks/running`, `/api/tasks/{id}`, `/api/tasks/{id}/logs`) every 1.5 s — stdlib only, nothing to install. The two gates are detected from the task's `status` + `reviewReason` (`plan_review` → early gate, `completed` → final gate).

---

## Capturing screenshots & a screencast (for docs / launch)

`scripts/demo-capture.mjs` drives the portal headlessly with Playwright and
produces PNG screenshots + an `.mp4` screencast of a task at any stage. Point it
at a task by a fragment of its title:

```bash
cd apps/frontend-web
node ../../scripts/demo-capture.mjs "ping" /tmp/aifactory-demo-shots
#                                    ^title  ^output dir
```

It logs into the SPA (token from `~/.aifactory/.token`), opens the matching task,
tours the **Overview / Subtasks / Files / Logs** tabs, and writes:

- `NN-*.png` — landing, board, task-detail (the *Build Ready for Review* gate with
  **Merge to main / Create PR / Request Changes**), and one shot per tab
- `demo-screencast.mp4` — H.264 1600×900 recording of the walkthrough

For the richest result, run it when the task is at the **review gate**
(`status: human_review`) so the diff summary and merge actions are on screen.

### Full-lifecycle screencast

`scripts/demo-capture-full.mjs` records the *whole* story in one pass and emits a
ready-to-share, variable-speed `lifecycle.mp4` plus milestone PNGs:

```bash
cd apps/frontend-web
node ../../scripts/demo-capture-full.mjs full /tmp/aifactory-demo-full
```

It (1) opens an empty board, (2) creates + starts a fresh task via the API,
(3) demos the **in-browser terminal** (opens *Terminal*, runs real `git log` /
`cat` commands), then (4) watches the card through every column to the
**Human Review** gate, grabbing a screenshot at each milestone
(`board-empty`, `task-added-started`, `terminal-commands`, `board-in-progress`,
`detail-logs`, `board-human-review`, `detail-review-gate`).

The build itself takes ~9 min, so **run it in the background** (it polls and
exits at the gate). On finish it auto-transcodes the raw recording into
`lifecycle.mp4` with **variable speed** — intro + terminal at 2×, the long
build middle squeezed to ~22 s, the review gate at 1× — using boundaries
captured from the real run (no hand-tuned timestamps). Override the task with
`DEMO_TITLE` / `DEMO_DESC` env vars.

Quick check of just the terminal step (no build): `… demo-capture-full.mjs terminal-test <dir>`.

**NixOS note (baked in):** the npm-resolved Playwright (1.60) expects a Chromium
build that isn't in the Nix store, and a downloaded one crashes on NixOS. The
script auto-selects the Nix-patched `chromium-1217` via `executablePath` from
`$PLAYWRIGHT_BROWSERS_PATH` — no `npx playwright install` needed. `ffmpeg` is
taken from `PATH` (or the Nix store) to transcode the recording to `.mp4`.

---

## Teardown / re-run

```bash
tmux kill-session -t aifactory-demo            # close the split screen
# re-running the launcher resets cleanly; add --no-provision to reuse the wired repo
```

The provisioning writes only inside `/tmp/aifactory-demo` (`.mcp.json`, `.aifactory-demo-mcp.sh`, `.claude/skills/handover/`). Nothing is written into the AIFactory repo itself.

---

## Troubleshooting

| Symptom | Fix |
|--------|-----|
| `Portal not reachable` | Start it: `cd apps/web-server && python -m server.main` |
| Feed stuck on "waiting for a handover" | The task hasn't started yet, or it errored before running — check the left pane / portal logs |
| Left pane has no `/handover` | Re-run without `--no-provision` (it copies the skill + `.mcp.json` into the demo repo) |
| Claude keeps prompting for tool permission | Expected the first time; approve, or pre-allow `mcp__aifactory__*` in the demo repo's Claude settings |
| Already inside tmux | The launcher creates a new session and prints `tmux attach -t aifactory-demo` |
| Right `║` of a banner looks 1 char short | Emoji are double-width in most terminals; purely cosmetic |
