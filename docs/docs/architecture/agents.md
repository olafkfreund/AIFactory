---
title: Agents
sidebar_position: 2
---

# Agents

AIFactory has nine agents grouped into two pipelines.

## Spec Creation Pipeline

Driven by `apps/backend/spec_runner.py`. Length is adaptive based on task complexity.

| Agent | Prompt | Job |
|---|---|---|
| **Gatherer** | `prompts/spec_gatherer.md` | Collects user requirements via Q&A |
| **Researcher** | `prompts/spec_researcher.md` | Validates external integrations (only for complex tasks) |
| **Writer** | `prompts/spec_writer.md` | Authors `spec.md` |
| **Critic** | `prompts/spec_critic.md` | Self-critique pass using ultrathink (only for complex tasks) |
| **Complexity Assessor** | `prompts/complexity_assessor.md` | AI-based complexity rating that drives pipeline length |

## Implementation Pipeline

Driven by `apps/backend/run.py`. Same agent classes every task.

| Agent | Prompt | Job |
|---|---|---|
| **Planner** | `prompts/planner.md` | Produces `implementation_plan.json` with subtasks |
| **Coder** | `prompts/coder.md` | Implements each subtask; can spawn subagents for parallel work |
| **Coder Recovery** | `prompts/coder_recovery.md` | Recovers stuck/failed subtasks (auto-invoked) |
| **QA Reviewer** | `prompts/qa_reviewer.md` | Validates the diff against the spec's acceptance criteria |
| **QA Fixer** | `prompts/qa_fixer.md` | Fixes QA-reported issues; loops with the reviewer until green |

## The BMad Method personas

For Level-2+ complexity tasks, agents adopt one of four BMad personas (defined in `apps/backend/integrations/bmad/personas/`):

- **Sarah (Planner)** — Staff PM, ruthless prioritizer
- **Winston (Architect)** — Principal Architect, security-conscious
- **Alex (Coder)** — Senior Developer, ultra-succinct
- **Jordan (QA)** — Lead QA Engineer, detail-oriented

These personas live in the system prompts and shape tone + decision making. They don't change tool permissions.

## Tool permissions per agent type

Defined in `apps/backend/core/client.py::create_client()`:

| Tool | Planner | Coder | QA Reviewer | QA Fixer |
|---|---|---|---|---|
| Read | ✓ | ✓ | ✓ | ✓ |
| Edit / Write | — | ✓ | — | ✓ |
| Bash (allowlisted) | — | ✓ | — | ✓ |
| WebSearch / WebFetch | — | ✓ | ✓ | — |
| MCP servers | ✓ | ✓ | ✓ | ✓ |

The Planner can read but never write code. The QA Reviewer can read but never write — only the Fixer applies changes the Reviewer requests.

## Sub-agents

The Coder agent can spawn sub-agents for parallel work via the SDK's nested invocation. Three named sub-agents under `apps/backend/integrations/bmad/subagents/`:

- `requirements_analyst` — validates a requirements doc (80% completeness, 100% clarity)
- `codebase_analyzer` — explores structure, detects tech stack, finds relevant files
- `technical_evaluator` — assesses risks, evaluates decisions

## Failure handling

If an agent's response is malformed (empty, missing required JSON keys), the runtime retries with a structured-output reminder appended to the prompt. After three retries, the task is paused and surfaced to the UI for human input.

If the LLM provider rate-limits, the runtime fails over to the next provider in the task's profile (see `apps/web-server/server/services/agent_service.py::_handle_rate_limit`).
