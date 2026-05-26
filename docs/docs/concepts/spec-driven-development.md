---
title: Spec-Driven Development
sidebar_position: 1
---

# Spec-Driven Development (SDD)

AIFactory builds software by writing a **spec** first, then executing against it. Every task moves through the same pipeline:

```
Discovery → Requirements → [Research] → Context → Spec → Plan → [Self-critique] → Validate → Code → QA
```

Phases in `[brackets]` only run for sufficiently complex tasks. The pipeline length is adaptive: a one-line typo fix runs 3 phases; a multi-service refactor runs all 8.

## Why this matters

Most AI coding tools start from a prompt and improvise. AIFactory starts from a written, reviewable artifact that:

- You can edit before code is written
- The QA agent compares against (acceptance criteria are first-class)
- Future agents can read (specs live in the repo, in `.aifactory/specs/`)

## The spec lives on disk

For every task:

```
.aifactory/specs/001-add-healthz-endpoint/
├── spec.md                  ← Markdown spec with acceptance criteria
├── requirements.json        ← Structured form of user input
├── context.json             ← Discovered codebase context
├── implementation_plan.json ← Subtask-based plan with status
└── qa_report.md             ← QA reviewer's verdict
```

These files are **version controlled in your repo**. The spec follows the code.

## Three agents

- **Planner** — owns the spec phases and produces the implementation plan
- **Coder** — implements each subtask, can spawn subagents for parallel work
- **QA Reviewer** — validates the diff against the acceptance criteria; if it fails, the **QA Fixer** loops until green

Each agent has its own prompt, model assignment, and tool permissions. See [Architecture: Agents](../architecture/agents) for the details.

## What you do

You write the spec. Or you let the planner draft one for you, then edit it. The portal's task-creation wizard accepts a one-line description and runs the discovery phases to fill in the rest. You approve or edit the resulting plan before any code is written.
