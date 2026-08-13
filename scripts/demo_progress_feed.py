#!/usr/bin/env python3
"""
AIFactory · live build feed — the right-hand pane of the split-screen demo.

Renders a continuously-updating, colourised view of ONE autonomous build as it
moves through AIFactory's pipeline:

    spec → plan → [REVIEW gate] → code → qa → [REVIEW gate] → done

It polls the portal's REST API (stdlib only — no third-party deps), so it is
bulletproof for a live demo. The two review gates ("handed back to you") are
called out with a banner, because those are the moments the developer steps
back in to drive the portal.

Usage:
    python demo_progress_feed.py [--portal URL] [--task TASK_ID]
                                 [--token-file PATH] [--interval SECONDS]

If --task is omitted, the feed waits for the first running task to appear
(i.e. the moment the developer runs /handover in the left pane) and locks onto
it. Once locked it keeps showing that task even after the agent stops, so the
final review/merge gate stays on screen.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
GREY = "\033[90m"
INVERSE = "\033[7m"

CLEAR = "\033[2J\033[H"  # clear screen + home
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def cols() -> int:
    return shutil.get_terminal_size((78, 24)).columns


# The canonical pipeline the audience follows. "review" gates are the beats
# where AIFactory hands control back to the developer.
PIPELINE = [
    ("spec", "spec"),
    ("plan", "planning"),
    ("REVIEW", "plan_review"),
    ("code", "coding"),
    ("qa", "validation"),
    ("REVIEW", "completed"),
    ("done", "done"),
]

# Map a raw phase/status to the pipeline index it lights up.
PHASE_TO_STAGE = {
    "spec_creation": 0,
    "spec": 0,
    "requirements": 0,
    "planning": 1,
    "plan": 1,
    "plan_review": 2,
    "coding": 3,
    "code": 3,
    "implementation": 3,
    "qa": 4,
    "qa_review": 4,
    "validation": 4,
    "qa_fixing": 4,
    "done": 6,
    "completed": 6,
}


# ---------------------------------------------------------------------------
# Portal client (stdlib)
# ---------------------------------------------------------------------------


class Portal:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def _get(self, path: str):
        url = f"{self.base}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def running(self) -> list[str]:
        # Primary: /api/tasks/running. On some builds a catch-all
        # GET /api/tasks/{task_id} route shadows it and returns 400, so we
        # fall back to scanning projects for active / review-gated tasks.
        try:
            data = self._get("/api/tasks/running")
            if isinstance(data, dict) and data.get("tasks"):
                return data["tasks"]
        except Exception as exc:
            sys.stderr.write(f"/api/tasks/running unavailable ({exc}); scanning projects\n")
        return self._scan_projects()

    def _scan_projects(self) -> list[str]:
        # A task is "interesting" to the feed if it's actively building OR
        # parked at a review gate (so the feed locks on at the gate too).
        active = {
            "in_progress",
            "human_review",
            "ai_review",
            "qa",
            "coding",
            "planning",
            "blocked",
        }
        out: list[str] = []
        try:
            projects = self._get("/api/projects")
        except Exception:
            return out
        if not isinstance(projects, list):
            return out
        for p in projects:
            pid = p.get("id") if isinstance(p, dict) else None
            if not pid:
                continue
            try:
                tasks = self._get(f"/api/projects/{pid}/tasks")
            except Exception:
                continue
            if not isinstance(tasks, list):
                continue
            for t in tasks:
                if (
                    isinstance(t, dict)
                    and (t.get("status") or "").lower() in active
                    and t.get("id")
                ):
                    out.append(t["id"])
        # Prefer real specs over the transient ":pending-..." placeholder.
        out.sort(key=lambda x: ":pending-" in x)
        return out

    def detail(self, task_id: str) -> dict | None:
        try:
            return self._get(f"/api/tasks/{task_id}")
        except Exception:
            return None

    def logs(self, task_id: str, limit: int = 12) -> list[str]:
        try:
            data = self._get(f"/api/tasks/{task_id}/logs?limit={limit}")
        except Exception:
            return []
        items = data.get("logs", data) if isinstance(data, dict) else data
        out: list[str] = []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, str):
                    out.append(it)
                elif isinstance(it, dict):
                    out.append(
                        str(
                            it.get("content")
                            or it.get("message")
                            or it.get("line")
                            or ""
                        )
                    )
        return [s for s in out if s.strip()]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def stage_index(detail: dict) -> int:
    """Best-effort: which pipeline stage is lit."""
    status = (detail.get("status") or "").lower()
    review = (detail.get("reviewReason") or "").lower()
    phase = (detail.get("phase") or "").lower()

    if status in ("done", "completed"):
        return 6
    if status == "human_review":
        # Which gate? plan_review = the early gate; anything else = final.
        return 2 if review == "plan_review" else 5
    if phase in PHASE_TO_STAGE:
        return PHASE_TO_STAGE[phase]
    if status == "in_progress":
        return 3  # assume coding if running with no clearer signal
    return 0


def render_pipeline(idx: int) -> str:
    parts = []
    for i, (label, _) in enumerate(PIPELINE):
        is_gate = label == "REVIEW"
        if i < idx:
            colour = GREEN
            mark = label
        elif i == idx:
            colour = (YELLOW if is_gate else CYAN) + BOLD
            mark = f"[{label}]" if is_gate else label
        else:
            colour = GREY
            mark = label
        parts.append(f"{colour}{mark}{RESET}")
    sep = f" {GREY}─{RESET} "
    return sep.join(parts)


def bar(pct: float, width: int = 28) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return f"{GREEN}{'█' * filled}{GREY}{'░' * (width - filled)}{RESET}"


def overall_pct(detail: dict, idx: int) -> float:
    if idx >= 6:
        return 100.0
    if idx == 5:
        return 95.0  # final review gate: build is done, awaiting merge
    subs = detail.get("subtasks") or []
    if subs:
        done = sum(
            1
            for s in subs
            if isinstance(s, dict)
            and (s.get("status") or "").lower() in ("completed", "done")
        )
        if done:
            # Coding spans the middle of the bar; scale subtask completion there.
            return 25 + 55 * (done / max(1, len(subs)))
    # Fall back to a coarse per-stage estimate.
    return {0: 8, 1: 18, 2: 22, 3: 45, 4: 85, 5: 95, 6: 100}.get(idx, 0)


SUB_ICON = {
    "completed": f"{GREEN}✓{RESET}",
    "done": f"{GREEN}✓{RESET}",
    "in_progress": f"{YELLOW}◐{RESET}",
    "failed": f"{RED}✗{RESET}",
    "pending": f"{GREY}○{RESET}",
}


def gate_banner(detail: dict, width: int) -> list[str]:
    status = (detail.get("status") or "").lower()
    review = (detail.get("reviewReason") or "").lower()
    if status not in ("human_review", "done", "completed"):
        return []

    if review == "plan_review":
        title = "⏸  HANDED BACK TO YOU — PLAN READY FOR REVIEW"
        body = "Open the portal → read the plan → Approve to start the build."
        colour = YELLOW
    elif review == "errors":
        title = "⚠  HANDED BACK TO YOU — NEEDS ATTENTION"
        body = "A phase failed. Open the portal → Logs to see what happened."
        colour = RED
    else:
        title = "✅  HANDED BACK TO YOU — BUILD COMPLETE"
        body = "Open the portal → Review the diff → Merge to land the code."
        colour = GREEN

    line = "═" * (width - 2)
    return [
        f"{colour}{BOLD}╔{line}╗{RESET}",
        f"{colour}{BOLD}║ {title.ljust(width - 4)} ║{RESET}",
        f"{colour}║ {DIM}{body.ljust(width - 4)}{RESET}{colour} ║{RESET}",
        f"{colour}{BOLD}╚{line}╝{RESET}",
    ]


def render(
    detail: dict | None,
    task_id: str | None,
    logs: list[str],
    portal: str,
    waiting: bool,
) -> str:
    w = min(cols(), 100)
    out = [CLEAR]
    host = portal.replace("http://", "").replace("https://", "")

    title = f"{BOLD}{MAGENTA}AIFACTORY{RESET}{BOLD} · LIVE BUILD FEED{RESET}"
    out.append(
        f"{title}{' ' * max(1, w - 32 - len(host) - 9)}{GREY}portal: {host}{RESET}"
    )
    out.append(f"{GREY}{'─' * w}{RESET}")

    if waiting or detail is None:
        out.append("")
        out.append(f"  {YELLOW}● waiting for a handover…{RESET}")
        out.append("")
        out.append(f"  {DIM}In the left pane, run:{RESET}")
        out.append(
            f"    {CYAN}/handover Add a /metrics endpoint returning request counts, with a test{RESET}"
        )
        out.append("")
        out.append(
            f"  {DIM}The moment the task starts, it locks on here and you'll see it build.{RESET}"
        )
        return "\n".join(out)

    idx = stage_index(detail)
    t = detail.get("title") or task_id or ""
    desc = (detail.get("description") or "").strip().replace("\n", " ")
    if len(desc) > w - 4:
        desc = desc[: w - 7] + "..."

    out.append(f"  {BOLD}task:{RESET} {CYAN}{task_id}{RESET}")
    out.append(f"  {DIM}{t}{RESET}")
    if desc:
        out.append(f"  {GREY}{desc}{RESET}")
    out.append("")

    out.append(f"  {render_pipeline(idx)}")
    out.append("")

    phase = detail.get("phase") or "-"
    status = detail.get("status") or "-"
    pct = overall_pct(detail, idx)
    running_dot = f"{GREEN}●{RESET}" if status == "in_progress" else f"{GREY}●{RESET}"
    out.append(
        f"  {running_dot} {BOLD}phase:{RESET} {phase:<14}  {BOLD}status:{RESET} {status}"
    )
    out.append(f"    {bar(pct)} {BOLD}{pct:4.0f}%{RESET}")
    out.append("")

    subs = detail.get("subtasks") or []
    if subs:
        out.append(f"  {BOLD}subtasks{RESET}")
        for s in subs[:8]:
            if not isinstance(s, dict):
                continue
            st = (s.get("status") or "pending").lower()
            icon = SUB_ICON.get(st, SUB_ICON["pending"])
            sid = s.get("id") or s.get("subtask_id") or "·"
            label = s.get("title") or s.get("description") or ""
            if len(label) > w - 12:
                label = label[: w - 15] + "..."
            colour = GREY if st == "pending" else ""
            out.append(
                f"    {icon} {colour}{str(sid):<5}{RESET}{colour} {label}{RESET}"
            )
        out.append("")

    banner = gate_banner(detail, w)
    if banner:
        out.extend("  " + b for b in banner)
        out.append("")

    if logs:
        out.append(f"  {GREY}┌ recent activity {'─' * (w - 20)}┐{RESET}")
        for line in logs[-10:]:
            line = line.rstrip()
            if len(line) > w - 6:
                line = line[: w - 9] + "..."
            out.append(f"  {GREY}│{RESET} {line}")
        out.append(f"  {GREY}└{'─' * (w - 4)}┘{RESET}")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def read_token(token_file: str) -> str:
    p = Path(os.path.expanduser(token_file))
    if not p.exists():
        sys.stderr.write(
            f"Token file not found: {p}\nStart the portal once to create it.\n"
        )
        sys.exit(1)
    return p.read_text().strip()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="AIFactory live build feed (demo right pane)."
    )
    ap.add_argument(
        "--portal", default=os.environ.get("AIFACTORY_PORTAL", "http://localhost:3101")
    )
    ap.add_argument(
        "--task", default=None, help="Lock onto a specific task id (project:spec)."
    )
    ap.add_argument("--token-file", default="~/.aifactory/.token")
    ap.add_argument("--interval", type=float, default=1.5)
    args = ap.parse_args()

    token = read_token(args.token_file)
    portal = Portal(args.portal, token)

    # Preflight: portal reachable?
    try:
        urllib.request.urlopen(f"{args.portal.rstrip('/')}/api/health", timeout=5)
    except Exception:
        sys.stderr.write(
            f"Portal not reachable at {args.portal}.\n"
            f"Start it: cd apps/web-server && python -m server.main\n"
        )
        return 1

    locked: str | None = args.task
    sys.stdout.write(HIDE_CURSOR)
    try:
        while True:
            if locked is None:
                running = portal.running()
                if running:
                    locked = running[0]
                else:
                    sys.stdout.write(render(None, None, [], args.portal, waiting=True))
                    sys.stdout.flush()
                    time.sleep(args.interval)
                    continue

            detail = portal.detail(locked)
            logs = portal.logs(locked)
            sys.stdout.write(render(detail, locked, logs, args.portal, waiting=False))
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write(SHOW_CURSOR + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
