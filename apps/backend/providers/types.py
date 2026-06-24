"""
QA Provider Message Protocol Types
====================================

Lightweight wrapper classes that satisfy the message protocol expected by
``reviewer.py`` and ``fixer.py``.  Both modules inspect messages exclusively
via ``type(obj).__name__`` string comparisons (never ``isinstance``), so
the *class name* must match exactly.

These classes are shared by all provider adapters so that any adapter can
yield compliant messages without depending on the real Claude SDK types.

See: .aifactory/specs/004-add-alternative-llm-for-qa-rev/abstraction_boundary.md
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Content blocks (items inside top-level messages)
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    """Plain text output from the LLM.

    Required by: ``reviewer.py``, ``fixer.py``
    Inspected via: ``type(block).__name__ == "TextBlock"``
    Accessed via: ``block.text``
    """

    text: str


@dataclass
class ToolUseBlock:
    """The LLM is calling a tool.

    Required by: ``reviewer.py``, ``fixer.py``
    Inspected via: ``type(block).__name__ == "ToolUseBlock"``
    Accessed via: ``block.name``, ``block.input`` (when verbose=True)
    """

    name: str
    input: dict = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    """Result of a tool call (appears inside ``UserMessage.content``).

    Required by: ``reviewer.py``, ``fixer.py``
    Inspected via: ``type(block).__name__ == "ToolResultBlock"``
    Accessed via: ``block.is_error``, ``block.content``
    """

    content: str | list
    is_error: bool = False


# ---------------------------------------------------------------------------
# Top-level messages (yielded by ``receive_response()``)
# ---------------------------------------------------------------------------


@dataclass
class AssistantMessage:
    """LLM assistant turn; contains one or more content blocks.

    Required by: ``reviewer.py``, ``fixer.py``
    Inspected via: ``type(msg).__name__ == "AssistantMessage"``
    Accessed via: ``msg.content`` (list of TextBlock / ToolUseBlock)
    """

    content: list


@dataclass
class UserMessage:
    """Tool-result turn (simulated user feedback).

    Required by: ``reviewer.py``, ``fixer.py``
    Inspected via: ``type(msg).__name__ == "UserMessage"``
    Accessed via: ``msg.content`` (list of ToolResultBlock)
    """

    content: list


@dataclass
class ResultMessage:
    """Terminal session message carrying real token usage.

    Mirrors the Claude SDK's ``ResultMessage`` so ``agents.session`` records
    per-turn token usage the SAME way for every provider: it inspects
    ``type(msg).__name__ == "ResultMessage"`` and reads ``msg.usage``
    (``{"input_tokens", "output_tokens", ...}``) plus ``msg.total_cost_usd`` /
    ``msg.session_id``. Agentic HTTP providers (e.g. openai-compatible / Ollama)
    yield ONE of these at the end of the loop so their usage lands in
    token_usage.json instead of being silently dropped (which made the benchmark's
    ``tokens > 0`` guard false-fail an Ollama build).
    """

    usage: dict[str, int]
    total_cost_usd: float | None = None
    session_id: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "AssistantMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
]
