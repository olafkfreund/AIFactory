"""Coder-only tool for recording a question and yielding the turn."""

import os
from pathlib import Path
from typing import Any

from agents.questions import (
    TASK_QUESTION_LIMIT,
    QuestionBudgetExhausted,
    question_run_id,
    write_question,
)

try:
    from claude_agent_sdk import tool
except ImportError:
    tool = None


def create_human_tools(
    spec_dir: Path,
    model: str,
    *,
    budget_spec_dir: Path | None = None,
    prior_task_count: int | None = None,
) -> list:
    if tool is None:
        return []

    @tool(
        "ask_human",
        "Record a question for the user and stop the coder turn.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "maxLength": 2000},
                "choices": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 200},
                    "minItems": 2,
                    "maxItems": 6,
                },
                "needs_approval": {"type": "boolean"},
                "context": {"type": "string", "maxLength": 4000},
                "default": {"type": "string", "maxLength": 500},
                "timeout_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            },
            "required": ["question"],
        },
    )
    async def ask_human(args: dict[str, Any]) -> dict[str, Any]:
        try:
            count = prior_task_count
            if count is None:
                raw_count = os.environ.get("AIFACTORY_TASK_QUESTION_COUNT", "")
                if raw_count and (
                    not raw_count.isascii()
                    or not raw_count.isdecimal()
                    or len(raw_count.lstrip("0")) > len(str(TASK_QUESTION_LIMIT))
                ):
                    raise QuestionBudgetExhausted(
                        "question budget exhausted: proceed on your best reading or stop"
                    )
                count = int(raw_count) if raw_count else 0
            write_question(
                spec_dir,
                question=args["question"],
                run_id=question_run_id(budget_spec_dir or spec_dir),
                asked_by_model=model,
                choices=args.get("choices"),
                needs_approval=args.get("needs_approval", False),
                context=args.get("context", ""),
                default=args.get("default"),
                timeout_minutes=args.get("timeout_minutes", 30),
                budget_spec_dir=budget_spec_dir,
                prior_task_count=count,
            )
        except (QuestionBudgetExhausted, ValueError, KeyError) as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Question recorded. Stop now: end your turn without further changes.",
                }
            ]
        }

    return [ask_human]
