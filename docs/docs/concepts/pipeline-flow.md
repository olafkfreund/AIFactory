---
title: Pipeline Flow — Assignment → Planning → Execution → Verification → Done
description: The end-to-end flow of a unit of work through AIFactory (and the PARR pipeline), and the precise definition of "done".
---

# Pipeline Flow

This is the canonical flow of a single unit of work: how it is **assigned**,
**planned**, **executed**, and **verified**, and exactly when we call it
**done**. It spans the PARR pipeline — **P**lan (PFactory), **A**ct
(AIFactory), **R**eview/verify (TFactory), all observed by CFactory — but
focuses on AIFactory's role and its handoffs.

## The flow

```mermaid
flowchart TD
    classDef gate fill:#fde68a,stroke:#b45309,color:#7c2d12;
    classDef done fill:#bbf7d0,stroke:#15803d,color:#14532d;
    classDef ext fill:#e0e7ff,stroke:#4338ca,color:#312e81;

    subgraph ASSIGN["1 · Task assignment"]
        A1["Source: PFactory governed plan,<br/>portal/CLI task, or GitHub issue"]
        A2{"Signed Task Contract v2?<br/>(RFC-0002)"}:::gate
        A1 --> A2
    end

    subgraph PLAN["2 · Planning"]
        P1["Spec pipeline<br/>(discovery → requirements → spec → plan)"]
        P2["Install plan verbatim<br/>(skip planning)"]
        P3["implementation_plan.json<br/>phases · subtasks · depends_on ·<br/>files · verification · required_commands"]
        P4{"Review tier<br/>(auto / async / blocking)"}:::gate
        P1 --> P3
        P2 --> P3
        P3 --> P4
    end

    A2 -- "no" --> P1
    A2 -- "yes: verify HMAC + completeness" --> P2

    P4 -- "blocking: high-risk / low confidence" --> H1["Human approves plan"]:::gate
    P4 -- "auto / async" --> EXEC
    H1 --> EXEC

    subgraph EXEC["3 · Execution"]
        E1{"parallel_safe phase<br/>& workers > 1?"}
        E2["Parallel waves<br/>(isolated sub-worktrees,<br/>merged sequentially)"]
        E3["Serial subtasks"]
        E4["Coder agent implements subtask<br/>(allowlist seeded from required_commands)"]
        E1 -- yes --> E2
        E1 -- no --> E3
        E2 --> E4
        E3 --> E4
    end

    subgraph VERIFY["4 · Verification"]
        V1["Per-subtask gates<br/>(test / lint / typecheck)"]
        V2{"passed?"}
        V3["Self-heal (optional):<br/>diagnose → retry → rollback → escalate"]:::gate
        V4["QA reviewer<br/>(acceptance criteria)"]
        V5{"QA pass?"}
        V6["QA fixer loop"]
        V7["Security pre-merge gate<br/>(secrets / injection)"]:::gate
        V1 --> V2
        V2 -- no --> V3 --> E4
        V2 -- yes --> V4 --> V5
        V5 -- no --> V6 --> V4
        V5 -- yes --> V7
    end

    E4 --> V1
    V7 -- "high-severity finding" --> H2["Human review / fix"]:::gate
    H2 --> E4
    V7 -- "clear" --> DONE

    subgraph DONEZONE["5 · Done"]
        DONE{"All subtasks completed<br/>+ QA criteria met<br/>+ gates clear?"}:::gate
        D1["Merge to base branch<br/>status: human_review → done"]:::done
        DONE -- yes --> D1
    end

    D1 --> T1["TFactory: generate + run tests<br/>(uses the tfactory profile)"]:::ext
    T1 --> T2{"Tests pass?"}:::ext
    T2 -- "no" --> HB["Handback → QA fixer"]:::ext
    HB --> E4
    T2 -- "yes" --> SHIP["Verified & shipped"]:::done
```

## What each stage means

1. **Task assignment.** Work enters from PFactory (a governed, signed plan), the
   portal/CLI, or a GitHub issue. If it carries a **signed Task Contract v2**
   (RFC-0002), AIFactory verifies the HMAC signature + completeness and installs
   the plan directly — *skipping its own planning*. Otherwise it runs the spec
   pipeline.
2. **Planning.** Produces `implementation_plan.json`: phases and subtasks with
   `depends_on`, file footprints, per-subtask `verification`, and the
   `required_commands` toolchain. A **review tier** is assigned — `auto`,
   `async`, or `blocking` — based on risk (auth/secrets/migrations/infra) and
   planner confidence.
3. **Execution.** Independent subtasks run as **parallel waves** in isolated
   sub-worktrees (merged sequentially) when the phase is `parallel_safe` and
   workers > 1; otherwise serially. The coder agent's command allowlist is
   seeded from the plan so it can verify its own work.
4. **Verification.** Each subtask runs its **gates** (test/lint/typecheck). With
   self-heal enabled, a failure triggers diagnose → bounded retry → rollback →
   escalate. Then the **QA reviewer** checks acceptance criteria (with a bounded
   QA-fixer loop), and a **security pre-merge gate** scans the diff.

## Definition of done

A unit is **done** only when *all* of these hold:

- **Every subtask is `completed`** in `implementation_plan.json` (no `pending`,
  `failed`, or orphaned work).
- **QA acceptance criteria pass** — the QA reviewer signs off (`qa_signoff`).
- **All gates are clear** — per-subtask verification passed, and the security
  pre-merge gate found nothing at/above the block threshold.
- **The review tier is satisfied** — any required human approval (blocking tier
  / high-risk paths) has been given.

At that point the branch merges to base and the task moves `human_review → done`.
**Shipped-and-verified** is the stronger bar: TFactory then generates and runs
tests from the contract's `tfactory` profile; only when those pass is the work
fully verified. Test failures hand back to the QA fixer (a bounded loop), never
silently.

## Why it is this way

- **Gates, not vibes.** "Done" is defined by *checks that ran* — subtask
  verification, QA criteria, security scan — not by an agent declaring success.
  Each gate is independent and auditable.
- **Skip planning when it's already been done.** PFactory does the governed
  analysis once; re-planning it in AIFactory wastes time and drifts. The signed
  contract lets AIFactory trust *and verify* that work and go straight to building.
- **Risk decides how much human is in the loop.** Trivial/trusted work flows
  `auto`; most work is `async` (humans comment without halting the agent);
  high-risk work *blocks* before code and before merge. The cost of review is
  paid where the risk is.
- **Parallelism without losing auditability.** Independent subtasks run
  concurrently in isolated worktrees and merge sequentially, so speed never
  costs a coherent, reviewable history.
- **Self-healing is bounded.** Retry/rollback is capped and checkpoint-backed;
  on exhaustion it escalates to a human rather than thrashing. Security and
  destructive triggers escalate immediately.

See also: [Task Lifecycle](../task-lifecycle.md) (every option/variable) and
[Task Contract v2](../task-contract.md) (the signed handoff).
