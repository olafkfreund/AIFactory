"""
Base provider strategy and shared types for multi-provider insights chat.

Outbound PII scrub (#1132)
--------------------------

Insights chat is the third outbound-LLM family in this repo. #1010 and
#1128 closed the other two (``providers/`` adapters and the Claude Agent
SDK call sites); the six strategies here still put the user's typed
message -- and the replayed conversation history -- on the wire verbatim.

``__init_subclass__`` wraps every subclass's ``send_message()``, which is
the ONE place a prompt enters a strategy: the CLI strategies stash it and
build argv later, the HTTP strategies stash it and build a JSON body
later. Wrapping here covers every strategy that exists and every strategy
added later, with no per-provider opt-in a new provider can forget --
the same chokepoint shape ``BaseLLMProvider.__init_subclass__`` uses.

The redaction itself is NOT reimplemented here. ``core.outbound_scrub``
in ``apps/backend`` is the single canonical implementation and the single
fail-closed contract; a second copy under ``apps/web-server`` would be
one edit away from drifting from the control it is supposed to mirror.
The web-server already depends on ``apps/backend`` this way
(``intake_poller``, ``conflict_service``, ``auto_fix_service``, and
``llm_audit_hook`` -- which imports the very same ``PiiRedactor``), so
this reuses an existing dependency rather than creating a new one.
"""

import abc
import functools
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# apps/backend holds the canonical outbound scrub. Mirror the path shim
# intake_poller.py already uses so the import below resolves the same way
# in the container, the dev runner and the tests.
_BACKEND_DIR = Path(__file__).resolve().parents[4] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@dataclass
class ProviderModel:
    """A model available from a provider."""

    id: str
    label: str


@dataclass
class ProviderInfo:
    """Detection result for a single provider."""

    provider: str
    available: bool
    display_name: str
    icon: str
    auth_method: str | None = None
    models: list[ProviderModel] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "available": self.available,
            "displayName": self.display_name,
            "icon": self.icon,
            "authMethod": self.auth_method,
            "models": [{"id": m.id, "label": m.label} for m in self.models],
        }


def scrub_insights_outbound(
    message: str,
    conversation_history: list[dict] | None,
    *,
    owner: str,
) -> tuple[str, list[dict] | None]:
    """Redact built-in PII from everything this call puts on the wire (#1132).

    Both arguments leave the process: the strategies append ``message`` to
    argv or to a JSON body, and the stateless ones replay
    ``conversation_history`` alongside it. Scrubbing only the new message
    would leak every prior turn on the next request.

    Fail-CLOSED (#320): if the canonical scrub cannot be imported at all,
    raise rather than fall through to sending raw text. Operators who want
    prompts sent unredacted set ``LITELLM_AUDIT_SCRUB_OUTBOUND=false`` --
    the one escape hatch, checked per call inside the scrub itself.
    """
    try:
        from core.outbound_scrub import (  # noqa: PLC0415
            scrub_outbound_enabled,
            scrub_outbound_prompt,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Outbound PII scrub is enabled but core.outbound_scrub could "
            f"not be imported from {_BACKEND_DIR}; refusing to send an "
            "unredacted insights prompt to the LLM provider (set "
            "LITELLM_AUDIT_SCRUB_OUTBOUND=false to disable outbound "
            "scrubbing)."
        ) from exc

    if not scrub_outbound_enabled():
        return message, conversation_history

    scrubbed = scrub_outbound_prompt(message, owner=owner)
    if not conversation_history:
        return scrubbed, conversation_history

    history = [
        {**turn, "content": scrub_outbound_prompt(turn["content"], owner=owner)}
        if isinstance(turn.get("content"), str) and turn["content"]
        else turn
        for turn in conversation_history
    ]
    return scrubbed, history


class ProviderStrategy(abc.ABC):
    """Abstract base class for insights chat providers.

    Outbound PII scrub (#1132): ``__init_subclass__`` wraps every
    subclass's ``send_message()`` so no strategy can put an unscrubbed
    prompt on the wire. See the module docstring for why the seam is here
    and not at the six call sites.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        send_message = cls.__dict__.get("send_message")
        if send_message is None or getattr(send_message, "__outbound_scrub__", False):
            return

        # The wrapper must mirror the ABC's signature exactly so callers can
        # pass these positionally or by keyword; the argument count is the
        # contract's, not a choice made here.
        @functools.wraps(send_message)
        async def _scrubbing_send_message(  # noqa: PLR0913
            self: Any,
            project_path: Path,
            project_id: str,
            message: str,
            model: str | None,
            model_config: dict | None,
            conversation_history: list[dict] | None,
        ) -> str:
            # Deliberately NOT mirroring BaseLLMProvider's
            # _outbound_prompt_raw / _outbound_prompt_scrubbed attributes.
            # Those adapters are constructed per call; these strategies are
            # registry singletons shared by every concurrent chat, so
            # per-request state on `self` would race between users. Nothing
            # needs them either: InsightsService persists the raw message to
            # the session before calling the provider, so "the UI keeps what
            # the user typed, only the wire is redacted" already holds.
            outbound, history = scrub_insights_outbound(
                message, conversation_history, owner=type(self).__name__
            )
            result: str = await send_message(
                self,
                project_path,
                project_id,
                outbound,
                model,
                model_config,
                history,
            )
            return result

        _scrubbing_send_message.__outbound_scrub__ = True  # type: ignore[attr-defined]
        cls.send_message = _scrubbing_send_message  # type: ignore[method-assign]

    @abc.abstractmethod
    async def detect(self) -> ProviderInfo:
        """Detect whether this provider is available and return its info."""
        ...

    @abc.abstractmethod
    async def send_message(
        self,
        project_path: Path,
        project_id: str,
        message: str,
        model: str | None,
        model_config: dict | None,
        conversation_history: list[dict] | None,
    ) -> str:
        """Send a message and stream the response via WebSocket events.

        Must broadcast insights:chunk events with types:
        text, tool_start, tool_end, done, error.

        Returns the full accumulated response text for persistence.
        """
        ...
