# AIFactory Self-Healing Build Pipeline — Design Spec

> Produced via /super-brainstorm. Decisions locked with the user (4 forks).
> Harness rule note: spec written here (plan file) because plan mode restricts
> edits; on implementation, copy to `docs/plans/2026-06-06-self-healing-pipeline-design.md`.

## Context

AIFactory builds software through coordinated Claude agent sessions
(plan → code → QA → human review). Recent work made parallel waves real and
reachable (#376/#389/#392), verifiable (#393), seedable for empty repos (#391),
and fast-pathable via signed plans (#390). The user wants the next evolution:
**use the best agentic-harness patterns from Antigravity, Codex, Copilot, and
Claude Code to make the pipeline faster, secure + profiled, and self-healing /
self-correcting** — without betting the architecture on external harnesses.

Live research (June 2026) confirms the patterns worth borrowing:
- **Antigravity 2.0**: parallel subagents + **Artifacts** (verifiable plan/diff/
  screenshot deliverables you comment on without halting the agent).
- **Claude Code**: **checkpoints + `/rewind`**, **hooks** (auto test/lint on
  change), background tasks, literal self-healing fallbacks.
- **Claude Agent SDK / Managed Agents**: compaction, crash recovery, "decouple
  brain from hands".
- **Codex (GPT-5.5) / Copilot**: parallel sandboxes + a **separate reviewer
  agent before commit/merge**.

## Locked decisions

1. **Harness role — adopt patterns, keep Claude execution.** Bring the patterns
   into AIFactory's existing executor; the provider abstraction
   (`providers/factory.py`: claude/codex/gemini/ollama/openai-compat) is
   unchanged. No new external-agent transport. (Forward-compatible: the
   self-heal verifier + security reviewer are subagents, so a future "external
   agent backend" seam can slot in later — out of scope now.)
2. **Human review — risk-tiered.** Route by risk + planner confidence:
   - **Auto-proceed**: #390 trusted-signed plans, solo, trivial.
   - **Async artifact review**: standard work — agent keeps building; human
     comments on Artifacts (plan, per-wave diff, QA report); feedback folded in
     between waves without halting.
   - **Blocking gate (pre-code AND pre-merge)**: high-risk (touches
     auth/secrets/migrations/infra) or low planner confidence, or high-severity
     security findings.
3. **Self-healing — bounded autonomy + checkpoint safety.** Checkpoint before
   each subtask/wave; on failure: diagnose → bounded retry with recovery hints
   (reuse `RecoveryManager`) → verifier (test/lint/QA) per unit → on exhaustion,
   roll back to last good checkpoint and escalate to the risk-tier gate.
   Security/destructive triggers escalate immediately.
4. **Secure + profile — build_report.json + security-reviewer gate.** Every
   build emits a profile (cost, tokens by phase, wall-clock per wave, parallel
   speedup vs serial baseline, QA rework rounds, checkpoints/rollbacks, self-heal
   events) by extending #393. A security-reviewer subagent scans the final diff
   (secrets, injection, authz, risky deps); high-severity → blocking pre-merge.

## Architecture (four pillars woven into the existing loop)

```
spec → PLAN ──risk classify──> [auto | async-artifact | blocking gate]
            │
            ▼  (per wave / subtask)
   checkpoint ──> CODE (waves #376) ──> verifier(hooks: test/lint) ──ok?──┐
        ▲              │ fail                                              │ok
        └── rollback ──┴── diagnose → bounded retry (RecoveryManager)     ▼
                                   │ exhausted → escalate           QA review/fix
                                   ▼                                      │
                              risk-tier gate                              ▼
                                                          security-reviewer (diff)
                                                                 │ high sev → gate
                                                                 ▼
                                                      build_report.json + merge
```

## Components (new / changed) — all in `apps/backend`

- **`risk_classifier.py` (new)** — pure: `classify(spec_dir, plan) -> RiskTier`
  using path globs (auth/secret/migration/infra), change size, planner
  confidence, and #390 trust. Drives the review tier. Heavily unit-tested.
- **`agents/checkpoint.py` (new)** — `Checkpointer(project_dir)`: `snapshot()`
  (git stash-ref or tag on the task branch before a wave) + `rollback(ref)`.
  Reuses git worktree primitives in `core/worktree.py`.
- **`agents/verifier.py` (new)** — per-unit hook runner (reuses
  `agents/gate_runner.py` `detect_gates`/`run_gates`): runs test/lint after a
  subtask, returns structured pass/fail fed into the self-heal loop.
- **`agents/self_heal.py` (new)** — orchestrates diagnose→retry→rollback→escalate
  around the existing serial + `run_parallel_phase` paths; wraps `RecoveryManager`
  and `Checkpointer`. The single place the loop's policy lives.
- **`agents/security_reviewer.py` (new)** — a subagent (via `create_client`,
  agent_type reviewer) that scans the final diff; returns findings with severity.
  Composes with the existing allowlist (defense in depth).
- **`agents/build_report.py` (new)** — extends `wave_log.py` (#393) into a full
  `build_report.json`: profile + self-heal events + security findings + speedup
  vs a recorded serial baseline. `load_build_report()` for the UI/benchmark.
- **`agents/artifacts.py` (new)** — emit/append Artifacts (plan, per-wave diff
  summary, QA report, security report) to `artifacts/` in the spec dir +
  mirror to source spec; the web UI renders them and accepts comments
  (async-review feedback channel, reusing the existing inbox `drain_inbox`).
- **Changed**: `agents/coder.py` `run_autonomous_agent` — insert checkpoint +
  verifier + self-heal hooks around wave/serial dispatch and the review gate;
  `review/state.py` — add risk-tier + pre-merge gate states; web-server
  `execution.py` — surface tier + artifacts + build_report endpoints.

## Cross-harness pattern → AIFactory mapping

| Pattern (source) | AIFactory landing |
|---|---|
| Artifacts, async comment (Antigravity) | `artifacts.py` + inbox feedback |
| Parallel subagents (Antigravity/Codex/Copilot) | already #376 waves |
| Checkpoints + rewind (Claude Code) | `checkpoint.py` |
| Hooks: test/lint on change (Claude Code) | `verifier.py` + `gate_runner` |
| Reviewer-agent before merge (Codex/Copilot) | `security_reviewer.py` |
| Crash recovery / decouple brain-hands (Agent SDK) | `self_heal.py` + RecoveryManager |

## Data artifacts (schemas)

- `build_report.json`: `{spec, parallel, workers_max, waves[], speedup_vs_serial,
  cost_usd, tokens_by_phase, qa_rounds, checkpoints[], rollbacks[],
  self_heal_events[], security_findings[], risk_tier, updated_at}`.
- `artifacts/`: `plan.md`, `wave-N-diff.md`, `qa_report.md`, `security_report.md`.

## Phased rollout (each independently shippable, behind flags)

1. **Profile + report** (`build_report.py` on #393) — measurable baseline first.
2. **Checkpoints + verifier** (`checkpoint.py`, `verifier.py`) — safety net.
3. **Self-heal loop** (`self_heal.py`) wiring into `coder.py`.
4. **Risk classifier + tiered review** (`risk_classifier.py`, review states).
5. **Security-reviewer gate** (`security_reviewer.py`).
6. **Artifacts + async feedback** (`artifacts.py` + UI).

## Verification

- Unit tests per pure module (classifier tiers, checkpoint snapshot/rollback,
  self-heal state machine, report schema, security-finding severity gating) —
  mirror the TDD style already used (#391/#393 test suites).
- E2E: a deliberately-failing subtask must auto-retry → rollback → escalate; a
  secrets-laden diff must trigger the pre-merge gate; a parallel build must
  emit `build_report.json` with `speedup_vs_serial > 1`.
- Benchmark: re-run the 11-subtask #376 plan; confirm report shows waves +
  speedup vs the 2026-06-05 serial baseline (~70 min / $11.46).

## Non-goals (YAGNI)

- No true external-agent delegation (Codex/Copilot/Antigravity as backends) —
  explicitly deferred behind a future seam.
- No new provider transport/auth. No replacing the QA loop — it's reused.
