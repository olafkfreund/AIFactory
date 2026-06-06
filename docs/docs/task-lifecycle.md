# Task Lifecycle — Creation & Execution Reference

This is the complete reference for **how an AIFactory task is created and driven
to completion**, and **every input, variable, option and setting** that controls
it. It is the contract a caller (a human in the portal, the CLI, or an upstream
system like PFactory) needs in order to create a task with all the information
AIFactory needs to run it all the way.

> For the *upstream* signed handoff that lets AIFactory skip planning entirely,
> see [Task Contract v2](./task-contract.md).

---

## 1. The pipeline at a glance

```
create task ─▶ SPEC (3–8 phases) ─▶ PLAN (implementation_plan.json)
            ─▶ review gate (tiered) ─▶ BUILD (coder, serial or parallel waves)
            ─▶ QA (review + fix loop) ─▶ done / human_review
```

- **Spec creation** (`apps/backend/runners/spec_runner.py`): a dynamic 3–8 phase
  pipeline scaled to complexity.
- **Build** (`apps/backend/run.py` → `agents/coder.py:run_autonomous_agent`):
  planner → coder (serial or parallel waves) → QA reviewer → QA fixer.
- Status is tracked in `implementation_plan.json` (`status`, `planStatus`) and
  surfaced to the portal Kanban.

---

## 2. Creating a task

### 2.1 Web API

**`POST /api/tasks/create-and-run`** (`apps/web-server/server/routes/execution.py`)
— `CreateAndRunRequest`:

| Field | Type | Req? | Notes |
|---|---|---|---|
| `title` | str | ✅ | Task title |
| `description` | str | ✅ | Task description (full; not truncated) |
| `complexity` | `simple`\|`standard`\|`complex` | ⬜ | Forces tier; else AI-assessed |
| `auto_continue` | bool | ⬜ | Default `true` — spec→plan→build in one run |
| `parallel` | bool | ⬜ | Enable parallel waves (→ `task_metadata.json`) |
| `workers` | int | ⬜ | Max concurrent subtasks (default 3) |
| `model` | str | ⬜ | Model override (shorthand or full id) |
| `mode` | `quick`\|`full` | ⬜ | Default `full`; auto-`quick` for `simple` |
| `baseBranch` | str | ⬜ | Git base for the worktree |
| `provenance` | object | ⬜ | Upstream traceability (see below) |

**`POST /api/tasks/from-plan`** — `FromPlanRequest`: ingest a **signed**
`implementation_plan.json` and skip the spec pipeline. See
[Task Contract v2](./task-contract.md).

**Provenance** (`TaskProvenance`, optional but recommended for upstream callers):
`session_id`, `issue_number`, `repo`, `source` (e.g. `pfactory`), `trusted_plan`,
`approved_by`, `approval_timestamp`, `plan_contract_version`, `signature`.

### 2.2 CLI

```bash
# Spec creation
python runners/spec_runner.py --task "Add rate limiting" [--complexity simple|standard|complex] [--no-ai-assessment]
# Build
python run.py --spec 001 [--model sonnet|opus|haiku|<id>] [--parallel] [--workers N]
python run.py --spec 001 --review | --merge | --discard
```

---

## 3. On-disk task artifacts

Per spec under `.aifactory/specs/<id>/`:

| File | Produced by | Carries |
|---|---|---|
| `requirements.json` | spec gatherer | `title`, `description`, `workflow_type`, `services_involved`, `metadata`, `provenance` |
| `task_metadata.json` | web-server on create | the execution settings (see §4) |
| `context.json` | discovery | `task_description`, `scoped_services`, `files_to_modify/reference`, `patterns` |
| `spec.md` | spec writer | Overview, Workflow Type, Task Scope, Success Criteria, QA Acceptance Criteria |
| `implementation_plan.json` | planner | the plan (see §6) |
| `qa_report.md` / `QA_FIX_REQUEST.md` | QA | acceptance results / fix requests |
| `parallel_report.json`, `build_report.json` | executor | wave/profile metrics |
| `.aifactory/.aifactory-security.json` | analyzer | the command allowlist (see §9) |

---

## 4. `task_metadata.json` — execution settings

The single place execution options live (`apps/backend/phase_config.py`
`TaskMetadataConfig`). All optional; sensible defaults apply.

| Field | Type | Meaning |
|---|---|---|
| `isAutoProfile` | bool | Use per-phase model/thinking maps |
| `phaseModels` | {phase: model} | Per-phase model override (wins when auto-profile) |
| `phaseThinking` | {phase: level} | Per-phase thinking level |
| `model` | str | Single model override (non-auto) |
| `thinkingLevel` | str | Single thinking level |
| `fastMode` | bool | Faster spec assessment |
| `soloMode` | bool | One self-directed agent (skips planner + gates + QA loop) |
| `parallel` | bool | Parallel waves |
| `workers` | int | Max concurrent subtasks |
| `mode` | `quick`\|`full` | Prompt/context depth |
| `complexity` | str | Tier (drives phase count) |
| `baseBranch` | str | Git base branch |
| `selectedSkills` | list | User-chosen skills |
| `suggestedSkills` | list | AI-suggested skills (#394) |

---

## 5. Model & provider selection

`apps/backend/phase_config.py` resolves the model per phase, in priority order:
**1)** per-phase `phaseModels` (if `isAutoProfile`) → **2)** CLI `--model` →
**3)** single `model` → **4)** `DEFAULT_PHASE_MODELS`.

- **Default phase models:** spec/planning/coding/qa/qa_fixer = `sonnet`.
- **Shorthand → id** (`MODEL_ID_MAP`): `opus`=`claude-opus-4-8`,
  `sonnet`=`claude-sonnet-4-6`, `haiku`=`claude-haiku-4-5-…`.
- **Thinking** (`THINKING_BUDGET_MAP`): `none`/`low`=1024/`medium`=4096/`high`=16384;
  default phase thinking = planning/qa `high`, coding/spec `medium`, qa_fixer `low`.
  Adaptive-thinking models (Opus 4.6+/Sonnet 4.6) use `{"type":"adaptive"}`.
- **Provider** is inferred from the model string
  (`phase_config.infer_provider_from_model`) and resolved by
  `providers/factory.py` — `claude` (default), `codex`, `antigravity`, `ollama`,
  `copilot`, `opencode`, `openai-compatible`, `gemini`. Never call the Anthropic
  SDK directly; route through `core.client.create_client` / `providers.factory`.

---

## 6. The implementation plan (`implementation_plan.json`)

| Level | Field | Notes |
|---|---|---|
| Plan | `feature`✅, `workflow_type`✅, `phases`✅ | `services_involved`, `final_acceptance`, `required_commands`, status fields |
| Phase | `phase`✅, `name`✅, `subtasks`✅ | `type`, `depends_on` (int or slug), `parallel_safe` |
| Subtask | `id`✅, `description`✅ | `status`, `depends_on`, `files_to_create`/`files_to_modify`, `model`, `patterns_from`, `verification`, `required_commands` |
| Verification | `type`✅ | `command`/`api`/`browser`/`component`/`manual`/`none`/`e2e` + `run`/`url`/`method`/`expect_*`/`scenario` |

- `depends_on` accepts integer phase numbers **or** planner slugs
  (`"phase-2-modules"`) — resolved to a phase number by the executor.
- `parallel_safe` + per-subtask `depends_on` + file footprints drive **wave
  scheduling** (#376).
- `required_commands` are verification command names seeded into the security
  allowlist before agents run (see §9).

---

## 7. Execution options

- **Parallel waves** (`--parallel --workers N`, or `task_metadata`): the wave
  scheduler groups non-dependent subtasks (by `depends_on` + file footprint) into
  concurrent waves run in isolated sub-worktrees, merged sequentially. Metrics in
  `parallel_report.json` / `build_report.json`.
- **Solo mode** (`AIFACTORY_SOLO_MODE` / `soloMode`): one agent plans+codes+
  verifies; skips the planner session, plan-review gate, and QA loop.
- **Auto-continue** (`auto_continue`, default on): runs spec→plan→build as one
  session; off pauses at gates.
- **Quick/full mode** (`mode`): prompt + context depth; `quick` auto-derived for
  `simple` complexity.
- **Complexity** (`simple`/`standard`/`complex`): controls the spec phase count
  (3 / 6–7 / 8). AI-assessed via `complexity_assessor.md` unless forced.

---

## 8. Agents, subagents & tools

Agent tool permissions are defined in `apps/backend/agents/tools_pkg/models.py`
and applied by `core/client.py`.

| Agent | Tools (summary) | Thinking default |
|---|---|---|
| `planner` | Read/Write/Bash/Web + Context7 + Graphiti + aifactory MCP | high |
| `coder` | Read/Write/Bash/Web + Context7 + Graphiti + aifactory MCP | none |
| `qa_reviewer` | + browser; aifactory `update_qa_status` | high |
| `qa_fixer` | + browser; `update_subtask_status` | medium |
| spec agents | gatherer/researcher/writer/critic (scoped read/write/web) | medium–high |

- **BMad subagents** (`integrations/bmad/subagents/`): `requirements_analyst`,
  `codebase_analyzer`, `technical_evaluator` — opt-in analysis helpers.
- **Skills** (`#394`): a deterministic matcher suggests `suggestedSkills` from the
  task; the planner may refine into `selectedSkills`.

---

## 9. Security profile (command allowlist)

`bash_security_hook` gates every Bash call against the profile at the agent's cwd
(`<cwd>/.aifactory/.aifactory-security.json` = `base | stack | script | custom`
commands). The profile is generated by `project/analyzer.py` (cached, mtime-
invalidated). A Python project always allowlists its verification toolchain
(`uv`/`pytest`/`ruff`/`mypy`/…), and a plan's `required_commands` are seeded into
`custom_commands` before agents run (serial/QA, each parallel sub-worktree, and
on signed-plan ingest). The SDK runs `bypassPermissions`, which does **not**
bypass this hook — the allowlist is the gate.

---

## 10. Review tiers & QA

- **Review tier** (`apps/backend/review_tier.py`): `auto` (trusted/solo/trivial),
  `async` (build continues, human comments fold in), `blocking` (gate before
  coding/merge). High-risk paths (auth, secrets, migrations, infra, payments) or
  low planner confidence → `blocking`.
- **QA**: `qa_reviewer` validates acceptance criteria → `qa_report.md`; on
  rejection `qa_fixer` resolves issues in a bounded loop. Gates use
  `gate_runner.detect_gates`/`run_gates`.

---

## 11. Required vs optional — minimum to drive a task end to end

**Required:** `title` + `description` (creation); for a plan: `feature`,
`workflow_type`, `phases`, and per subtask `id` + `description`. For the
skip-planning fast-path: a valid `approval` signature + completeness (see
[Task Contract v2](./task-contract.md)).

**Optional but high-value:** `complexity`, `parallel`/`workers`, `model`/
`phaseModels`, `thinkingLevel`, `mode`, per-subtask `depends_on` +
`files_to_*` (wave scheduling), `verification`, `required_commands`, `provenance`.
