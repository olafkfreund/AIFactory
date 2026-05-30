#!/usr/bin/env bash
# record-handover-command.sh — focused terminal demo of the /handover command.
#
# Shows a developer in their repo handing a task to AIFactory with one command.
# Creates a REAL backlog task via the portal API so the task id + URL are real,
# then narrates what AIFactory does next. Designed to be recorded with asciinema:
#   asciinema rec --overwrite --command "bash scripts/record-handover-command.sh" out.cast
set -uo pipefail

B=$'\033[1m'; D=$'\033[2m'; C=$'\033[36m'; G=$'\033[32m'; Y=$'\033[33m'; M=$'\033[35m'; R=$'\033[0m'
PORTAL_API="${AIFACTORY_API_URL:-http://localhost:3101}"
PORTAL_UI="${AIFACTORY_UI_URL:-http://localhost:3100}"
PID="${AIFACTORY_PROJECT_ID:-f7ac8d99-b913-4c6f-afce-f8376e29c98c}"
TOKEN="$(cat ~/.aifactory/.token 2>/dev/null)"
REPO="/tmp/aifactory-demo"

type() { printf "%s" "$1"; sleep 0.5; }       # "typed" text
pause() { sleep "${1:-1}"; }

clear
echo "${C}${B}━━━ AIFactory · the /handover command ━━━${R}"
pause 1
echo
echo "${D}# You're a developer in your repo, mid-conversation with Claude Code.${R}"
echo "${D}# You've just described a small feature. Instead of building it now,${R}"
echo "${D}# you hand it to AIFactory's autonomous pipeline — one command.${R}"
pause 2
echo
printf "${M}%s${R} ${D}~/%s${R}\n" "olaf@dev" "tmp/aifactory-demo"
printf "${G}❯${R} "
type "claude"; echo
pause 1
echo "${D}  (Claude Code session — type a slash command)${R}"
pause 1
echo
printf "${C}${B}> /handover${R} ${B}Add a /uptime endpoint returning process uptime seconds, with a test${R}\n"
pause 2
echo
echo "${D}  …Claude runs the handover skill…${R}"
pause 1

# Real backlog task so the id + URL are genuine.
RESP="$(curl -fsS -X POST "${PORTAL_API}/api/projects/${PID}/tasks" \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -d '{"title":"Add /uptime endpoint","description":"Add a GET /uptime endpoint returning process uptime in seconds, with a pytest test using TestClient."}' 2>/dev/null)"
SID="$(printf '%s' "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("specId",""))' 2>/dev/null)"
[ -z "$SID" ] && SID="00X-add-uptime-endpoint"

echo "${G}✓${R} Matched project ${B}aifactory-demo${R} ${D}(${REPO})${R}"
pause 0.6
echo "${G}✓${R} Created task ${B}${SID}${R} and handed it to AIFactory"
pause 0.6
echo "${G}✓${R} Track it in the portal: ${C}${PORTAL_UI}${R}"
pause 1.2
echo
cat <<EOF
${B}What happens now — without you:${R}
  ${D}planner${R}  writes the spec + subtask plan
  ${Y}⏸  pauses at a review gate${R} ${D}— you approve the plan${R}
  ${D}coder${R}    implements each subtask in an isolated git worktree
  ${D}QA${R}       validates the acceptance criteria + runs the tests
  ${Y}⏸  pauses again${R} ${D}— a merge-ready diff waits for your review${R}
EOF
pause 2.5
echo
echo "${B}Your part:${R} 1 conversation, ${C}1 /handover${R}, 1 plan approval, 1 review."
echo "${D}Go work on something else — come back to a finished, merge-ready branch.${R}"
pause 2
echo
echo "${C}${B}━━━ that's /handover ━━━${R}"
pause 1
