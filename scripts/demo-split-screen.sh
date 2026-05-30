#!/usr/bin/env bash
#
# AIFactory split-screen live demo.
#
# Shows the full developer ⇄ AIFactory loop on two synchronized screens:
#
#   ┌──────────────────────────┬──────────────────────────┐
#   │  CLAUDE CODE (real CLI)   │  AIFACTORY · live build   │   + the portal
#   │  you run /handover here   │  feed (right tmux pane)   │     web UI opens
#   └──────────────────────────┴──────────────────────────┘     in your browser
#
# User story it tells:
#   1. A developer is working in a repo (the public olafkfreund/aifactory-demo).
#   2. They hand a task to AIFactory with  /handover  (left pane).
#   3. AIFactory plans → pauses for plan review → codes → QA's → pauses for
#      final review (right pane + portal follow every step live).
#   4. The developer reviews the diff and merges (in the portal).
#
# This script:
#   - preflights the portal, token, claude, tmux, venv
#   - provisions /tmp/aifactory-demo so its Claude Code session has /handover
#     and the aifactory MCP tools
#   - registers the repo with the portal
#   - launches a tmux split: left = real Claude Code, right = the live feed
#   - opens the portal in your browser
#
# Prereqs (fails fast if missing):
#   - tmux, claude, jq, curl
#   - Portal running:   cd apps/web-server && python -m server.main   (port 3101)
#   - Frontend running: cd apps/frontend-web && npm run dev           (port 3100)
#   - ~/.aifactory/.token  (created by the portal on first boot)
#   - gh CLI authenticated (only needed to clone the demo repo the first time)
#
# Flags:
#   --portal=URL     portal base URL (default http://localhost:3101)
#   --frontend=URL   portal web UI URL (default http://localhost:3100)
#   --repo-local=DIR local clone of the demo repo (default /tmp/aifactory-demo)
#   --no-provision   skip cloning/registering/wiring (reuse an existing setup)
#   --no-browser     don't open the browser
#   --no-attach      create the tmux session but don't attach (print attach cmd)
#   --help           print this help and exit
#

set -euo pipefail

# ---------- resolve repo root ----------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------- defaults + flags ----------

PORTAL="http://localhost:3101"
FRONTEND="http://localhost:3100"
DEMO_REPO="olafkfreund/aifactory-demo"
DEMO_LOCAL="/tmp/aifactory-demo"
SESSION="aifactory-demo"
DEMO_TASK="Add a /metrics endpoint returning request counts, with a test"
PROVISION=1
OPEN_BROWSER=1
ATTACH=1

print_help() { sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
  case "$arg" in
    --portal=*)    PORTAL="${arg#--portal=}" ;;
    --frontend=*)  FRONTEND="${arg#--frontend=}" ;;
    --repo-local=*) DEMO_LOCAL="${arg#--repo-local=}" ;;
    --no-provision) PROVISION=0 ;;
    --no-browser)  OPEN_BROWSER=0 ;;
    --no-attach)   ATTACH=0 ;;
    --help|-h)     print_help; exit 0 ;;
    *) echo "Unknown flag: $arg" >&2; print_help; exit 1 ;;
  esac
done

# ---------- output helpers ----------

C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[32m'
C_YELLOW='\033[33m'; C_RED='\033[31m'; C_CYAN='\033[36m'

step() { echo; echo -e "${C_BOLD}${C_CYAN}=== $1 ===${C_RESET}"; }
ok()   { echo -e "  ${C_GREEN}✓${C_RESET} $1"; }
warn() { echo -e "  ${C_YELLOW}⚠${C_RESET} $1"; }
die()  { echo -e "  ${C_RED}✗${C_RESET} $1" >&2; exit "${2:-1}"; }

# ---------- preflight ----------

step "Preflight"

command -v tmux  >/dev/null || die "tmux not found." 1
command -v claude >/dev/null || die "claude CLI not found (https://claude.com/claude-code)." 1
command -v curl  >/dev/null || die "curl not found." 1
command -v jq    >/dev/null || die "jq not found." 1
ok "tmux, claude, curl, jq present"

PY="$ROOT/apps/backend/.venv/bin/python"
[ -x "$PY" ] || die "backend venv missing at $PY — run: npm run install:backend" 1
ok "backend venv present"

curl -fsS "${PORTAL}/api/health" >/dev/null 2>&1 \
  || die "Portal not reachable at ${PORTAL}. Start it: cd apps/web-server && python -m server.main" 1
ok "Portal up at ${PORTAL}"

curl -fsS -o /dev/null "${FRONTEND}/" 2>/dev/null \
  && ok "Frontend up at ${FRONTEND}" \
  || warn "Frontend not reachable at ${FRONTEND} (start it: cd apps/frontend-web && npm run dev)"

TOKEN_FILE="${HOME}/.aifactory/.token"
[ -f "$TOKEN_FILE" ] || die "Token not found at ${TOKEN_FILE}. Boot the portal once to create it." 1
TOKEN="$(cat "$TOKEN_FILE")"
ok "Token loaded"

AUTH="Authorization: Bearer ${TOKEN}"
CT="Content-Type: application/json"

HANDOVER_SKILL="$ROOT/.claude/skills/handover"
[ -d "$HANDOVER_SKILL" ] || die "handover skill missing at $HANDOVER_SKILL" 1

if [ -n "${TMUX:-}" ]; then
  warn "You're already inside tmux — the demo will create a NEW session you attach to."
  ATTACH=0
fi

# ---------- provision the demo repo ----------

if [ "$PROVISION" = "1" ]; then
  step "Provision ${DEMO_REPO}"

  if [ ! -d "$DEMO_LOCAL/.git" ]; then
    command -v gh >/dev/null || die "gh CLI needed to clone ${DEMO_REPO} the first time." 1
    gh auth status >/dev/null 2>&1 || die "gh not authenticated. Run: gh auth login" 1
    ok "Cloning ${DEMO_REPO} → ${DEMO_LOCAL}"
    gh repo clone "$DEMO_REPO" "$DEMO_LOCAL" >/dev/null 2>&1 \
      || die "clone failed. Create it: gh repo create ${DEMO_REPO} --public" 1
  else
    git -C "$DEMO_LOCAL" pull --quiet 2>/dev/null || true
    ok "Reusing existing clone at ${DEMO_LOCAL}"
  fi

  # Register the project with the portal (idempotent).
  REG=$(curl -sS -w '\n%{http_code}' -X POST "${PORTAL}/api/projects" \
        -H "$AUTH" -H "$CT" \
        -d "{\"path\":\"${DEMO_LOCAL}\",\"name\":\"aifactory-demo\"}")
  REG_BODY=$(echo "$REG" | head -n -1); REG_CODE=$(echo "$REG" | tail -n1)
  if [ "$REG_CODE" = "201" ]; then
    ok "Registered project ($(echo "$REG_BODY" | jq -r '.id'))"
  elif [ "$REG_CODE" = "409" ]; then
    ok "Project already registered"
  else
    die "Register failed (HTTP ${REG_CODE}): $REG_BODY" 2
  fi

  # Wire the aifactory MCP server + /handover skill INTO the demo repo so the
  # left-pane Claude Code session can hand tasks off. The wrapper forces
  # CLAUDE_PROJECT_DIR back to THIS repo so start-aifactory-mcp.sh finds the venv.
  WRAP="$DEMO_LOCAL/.aifactory-demo-mcp.sh"
  cat > "$WRAP" <<EOF
#!/usr/bin/env bash
# Generated by demo-split-screen.sh — runs the aifactory MCP server for the demo.
export CLAUDE_PROJECT_DIR="$ROOT"
exec bash "$ROOT/scripts/start-aifactory-mcp.sh" "\$@"
EOF
  chmod +x "$WRAP"

  cat > "$DEMO_LOCAL/.mcp.json" <<EOF
{
  "mcpServers": {
    "aifactory": {
      "type": "stdio",
      "command": "bash",
      "args": ["$WRAP"],
      "env": {
        "AIFACTORY_API_URL": "$PORTAL",
        "AIFACTORY_API_TOKEN_FILE": "~/.aifactory/.token"
      }
    }
  }
}
EOF
  ok "Wrote .mcp.json + MCP wrapper into the demo repo"

  mkdir -p "$DEMO_LOCAL/.claude/skills"
  rm -rf "$DEMO_LOCAL/.claude/skills/handover"
  cp -r "$HANDOVER_SKILL" "$DEMO_LOCAL/.claude/skills/handover"
  ok "Installed /handover skill into the demo repo"
else
  step "Provision — skipped (--no-provision)"
  [ -d "$DEMO_LOCAL" ] || die "No demo repo at ${DEMO_LOCAL}; run without --no-provision first." 1
fi

# ---------- launch tmux split-screen ----------

step "Launch split-screen (tmux session: ${SESSION})"

tmux kill-session -t "$SESSION" 2>/dev/null || true

# Use pane IDs (%N) throughout — they are immune to base-index/pane-base-index
# config (hardcoding :0.0 breaks when the user sets base-index 1).

# Left pane: a real Claude Code session in the developer's repo.
tmux new-session -d -s "$SESSION" -c "$DEMO_LOCAL"
LEFT_PANE="$(tmux display-message -p -t "$SESSION" '#{pane_id}')"
tmux send-keys -t "$LEFT_PANE" \
  "clear; printf '\033[1;36m  You are the developer. Hand the task to AIFactory:\033[0m\n\n    \033[1m/handover ${DEMO_TASK}\033[0m\n\n  \033[2mStarting Claude Code…\033[0m\n'; sleep 1; claude" C-m

# Right pane: the live build feed (stdlib python; backend venv is fine).
RIGHT_PANE="$(tmux split-window -h -P -F '#{pane_id}' -t "$LEFT_PANE" -c "$ROOT" \
  "exec '$PY' '$ROOT/scripts/demo_progress_feed.py' --portal '$PORTAL'")"

# Cosmetics: titled borders, focus the left pane.
tmux set-option -t "$SESSION" pane-border-status top 2>/dev/null || true
tmux select-pane -t "$LEFT_PANE"  -T " ① CLAUDE CODE — you (run /handover) " 2>/dev/null || true
tmux select-pane -t "$RIGHT_PANE" -T " ② AIFACTORY — live build feed " 2>/dev/null || true
tmux select-pane -t "$LEFT_PANE"
ok "tmux session ready (left: Claude Code, right: live feed)"

# ---------- open the portal ----------

if [ "$OPEN_BROWSER" = "1" ]; then
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$FRONTEND/" >/dev/null 2>&1 || true
  elif command -v open  >/dev/null 2>&1; then open "$FRONTEND/" || true; fi
  ok "Opened portal web UI: ${FRONTEND}/  (tile this window next to the terminal)"
fi

# ---------- talk track + attach ----------

step "You're live"
cat <<EOF

  Layout:  [ tmux: Claude Code | live feed ]   +   [ browser: portal kanban ]
  Tile the terminal and browser side-by-side for the full two-screen effect.

  Run the demo (read this top-to-bottom while it plays):

   1. LEFT pane  → type:   /handover ${DEMO_TASK}
                   (approve the aifactory MCP tools when Claude asks)

   2. RIGHT pane → the feed locks on, shows: spec → plan → [REVIEW] …
      PORTAL     → the task appears on the board and starts moving.

   3. PLAN-REVIEW GATE — both screens show "⏸ HANDED BACK TO YOU".
      In the portal, open the task → read the plan → click Approve.
      (You are the human gate. This is the hand-back-for-review beat.)

   4. AIFactory codes + QA's. Watch subtasks tick green in the feed and
      the portal's Subtasks/Logs tabs.

   5. FINAL-REVIEW GATE — "✅ BUILD COMPLETE".
      In the portal: open the task → Review tab → inspect the diff → Merge.
      The agent's commits land on your local branch. Loop closed.

  Manage the session:
    tmux attach -t ${SESSION}      # reconnect if detached
    tmux kill-session -t ${SESSION} # tear it down

  Full runbook + narration: guides/demo-split-screen.md
EOF

if [ "$ATTACH" = "1" ]; then
  exec tmux attach -t "$SESSION"
else
  echo
  ok "Session created. Attach with:  tmux attach -t ${SESSION}"
fi
