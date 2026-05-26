---
title: Glossary
sidebar_position: 3
---

# Glossary

**Spec** — A markdown document at `.aifactory/specs/<spec-id>/spec.md` with description + acceptance criteria. The source of truth for a task.

**Spec-id** — A `NNN-<slug>` identifier, e.g., `001-add-healthz-endpoint`. Lexicographically ordered.

**Task-id** — A composite `<project-id>:<spec-id>`. Used in API URLs.

**Subtask** — A line item in the implementation plan. Each has a status (`pending`, `in_progress`, `done`, `blocked`).

**Worktree** — A git working directory under `.aifactory/worktrees/tasks/<spec-id>/`. Isolated from the user's main tree.

**Agent Profile** — A reusable mapping of phases to model strings. Stored in `~/.aifactory/profiles.json` and selectable from the Task Creation Wizard.

**Phase** — A discrete step in the pipeline. Spec creation has up to 7 phases; implementation has 3 (Planning, Coding, QA).

**Phase Model** — The model string for a given phase, e.g., `sonnet`, `ollama:qwen3:14b`, `gpt-4o`.

**MCP** — Model Context Protocol. The standard way agents discover and call external tools. AIFactory ships with Context7, Linear, Graphiti, Puppeteer MCP servers wired up by default.

**Graphiti** — The semantic memory layer. A knowledge graph stored in an embedded LadybugDB instance under `.aifactory/specs/<spec-id>/graphiti/`. Used by the planner to pull in prior-session insights.

**rmux** — A Rust-based fork of tmux that AIFactory bundles for the Live Agent Console. Provides byte-level pane streaming via `pipe-pane`.

**rmux session** — A tmux-style session managed by the rmux daemon. AIFactory creates one per task when `AIFACTORY_RMUX_ENABLED=true`.

**Attach** — Taking interactive control of an agent's rmux session via the browser. Exclusive — only one client at a time (409 for others).

**Auto-profile** — A task-level setting that lets the planner pick provider/model per phase based on inferred complexity. Overrides the user's default profile.

**BMad Method** — A scale-adaptive intelligence framework integrated in `apps/backend/integrations/bmad/`. Provides complexity detection (5 levels), three-track planning, and four named agent personas (Sarah/Winston/Alex/Jordan).

**Failover** — Automatic provider switching when the active provider rate-limits or errors. Order of attempts is the task's profile list, in declared order.
