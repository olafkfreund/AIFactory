#!/usr/bin/env bash
#
# create-pfactory-labels.sh — create the PFactory tag-taxonomy (v1) labels.
#
# PFactory emits governed GitHub epics + child issues that AIFactory picks up.
# Those issues are tagged with a shared "tag taxonomy" (the secret language
# between the two systems). This script creates the PFactory-specific labels
# in a repo so emitted issues carry valid labels. See epic #327 / issue #328
# and guides/pfactory-tag-taxonomy.md for the full contract.
#
# Idempotent: uses `gh label create --force`, so re-running updates colour /
# description in place rather than failing on existing labels.
#
# Reused labels (epic, sev:*, backend/frontend/mcp/security) are intentionally
# NOT created here — they already exist in this repo. See the doc note.
#
# Usage:
#   scripts/create-pfactory-labels.sh                 # current repo (gh default)
#   scripts/create-pfactory-labels.sh --repo owner/name
#   scripts/create-pfactory-labels.sh --dry-run       # print, don't create
#
set -euo pipefail

REPO=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --repo=*) REPO="${1#--repo=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

command -v gh >/dev/null || { echo "gh CLI not found: https://cli.github.com/" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh not authenticated. Run: gh auth login" >&2; exit 1; }

REPO_ARGS=()
[ -n "$REPO" ] && REPO_ARGS=(--repo "$REPO")

# name|color|description  — the PFactory-specific taxonomy (v1).
# Colours mirror issue #328's table.
LABELS=(
  # Mandatory marker — every PFactory-emitted issue carries this.
  "pfactory|5319E7|Created by PFactory (governed: AI gates passed + human approved)"

  # Routing — which downstream system executes the work.
  "handoff:aifactory|1D76DB|Route to AIFactory for execution"
  "handoff:tfactory|0E8A16|Route to TFactory for test generation"

  # Work category (type:*). type:feature already exists in this repo and is
  # reused; --force keeps it idempotent if present.
  "type:software|C5DEF5|PFactory work category: software"
  "type:feature|C5DEF5|PFactory work category: feature"
  "type:infra|C5DEF5|PFactory work category: infrastructure"
  "type:hosting|C5DEF5|PFactory work category: hosting"
  "type:testing|C5DEF5|PFactory work category: testing"
  "type:cicd|C5DEF5|PFactory work category: CI/CD"
  "type:product|C5DEF5|PFactory work category: product"

  # Plan-type descriptor.
  "plan-type:software-service|BFDADC|PFactory plan-type: software service"
  "plan-type:data-pipeline|BFDADC|PFactory plan-type: data pipeline"
  "plan-type:infra-change|BFDADC|PFactory plan-type: infra change"
  "plan-type:generic-deliverable|BFDADC|PFactory plan-type: generic deliverable"

  # Execution priority (p0 critical-path → p3 lowest). Distinct from this
  # repo's legacy priority:high/medium/low scheme.
  "priority:p0|D93F0B|PFactory execution priority p0 (critical path, first)"
  "priority:p1|E99695|PFactory execution priority p1 (high)"
  "priority:p2|FBCA04|PFactory execution priority p2 (medium)"
  "priority:p3|0E8A16|PFactory execution priority p3 (low)"
)

echo "Creating ${#LABELS[@]} PFactory taxonomy labels${REPO:+ in $REPO}..."
for spec in "${LABELS[@]}"; do
  IFS='|' read -r name color desc <<<"$spec"
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $name (#$color) — $desc"
    continue
  fi
  gh label create "$name" --color "$color" --description "$desc" --force "${REPO_ARGS[@]}"
done

echo "Done. Reused labels (not created here): epic, sev:critical|high|medium|low, backend, frontend, mcp, security."
