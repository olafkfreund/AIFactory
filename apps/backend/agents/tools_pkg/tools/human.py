"""Coder-only tool for recording a question and yielding the turn."""

from pathlib import Path
from typing import Any

from agents.questions import QuestionBudgetExhausted, question_run_id, write_question

try:
    from claude_agent_sdk import tool
except ImportError:
    tool = None


def create_human_tools(
    spec_dir: Path, model: str, *, budget_spec_dir: Path | None = None
) -> list:
    if tool is None:
        return []

    @tool(
        "ask_human",
        "Record a question for the user and stop the coder turn.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "choices": {"type": "array", "items": {"type": "string"}},
                "needs_approval": {"type": "boolean"},
                "context": {"type": "string"},
                "default": {"type": "string"},
                "timeout_minutes": {"type": "integer"},
            },
            "required": ["question"],
        },
    )
    async def ask_human(args: dict[str, Any]) -> dict[str, Any]:
        try:
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
