"""
Mention Parsing & Resolution — ``#task`` / ``@agent`` references (#273)
=======================================================================

Parses structured *mentions* out of arbitrary message / comment text and turns
them into resolvable references. Pairs with the #264 inbox: a message can
``@``-address a recipient (route it to that agent) and ``#``-link one or more
tasks (attach those task references to the stored payload).

Two token kinds are recognised:

* ``#<task-id>``  → a **task** reference (links the message to a task / spec).
* ``@<name>``     → a **mention** of an agent / team / recipient.

Design constraints
------------------
* **Pure & dependency-free.** Only the standard library ``re`` is used. This
  module performs *no* IO and imports nothing from the agents package, so the
  web-server can load it as a self-contained unit (the shared contract is this
  parser's behaviour, not a package dependency).
* **Resolution never throws** on an unknown id/name — unknown refs come back
  marked ``resolved=False`` so callers can decide what to do.

Matching rules (and the false-matches we deliberately avoid)
-----------------------------------------------------------
A token is only accepted when its ``#`` / ``@`` sigil is *not* part of a larger
lexical construct. The scanner first masks out regions where a sigil must be
ignored, then matches tokens in the remaining (visible) text.

Masked / ignored regions:

1. **Fenced code blocks** — text between triple backticks (```` ``` ````), and
   **inline code** — text between single backticks (`` `…` ``). Mentions inside
   code are documentation/examples, never live references.
2. **URLs** — a ``#`` that is part of a URL fragment (e.g.
   ``https://example.com/p#section``) is ignored. We mask from a ``scheme://``
   up to the next whitespace.

Token-level guards (applied to the visible text):

3. **Email addresses** — ``@`` is only a mention when it is *not* preceded by a
   word character. ``a@b.com`` / ``user@host`` therefore never match, while
   ``@coder`` and ``hey @coder`` do.
4. **``##`` headings / repeated sigils** — a ``#`` is only a task ref when it is
   *not* preceded by ``#`` (so Markdown ``## Heading`` and ``###`` are skipped)
   and not preceded by a word character (so ``foo#3`` does not match).
5. **Valid id/name body** — the token body must be a plausible identifier:

   * task id  : starts alphanumeric, then alphanumerics / ``._-`` ending on an
     alphanumeric (e.g. ``001``, ``001-feature``, ``ABC-123``, ``v1.2``). A bare
     ``#`` followed by a space (a heading) cannot match.
   * mention  : same shape (e.g. ``coder``, ``qa_reviewer``, ``team-x``).
     Trailing punctuation is excluded.

Returned refs carry ``start``/``end`` offsets into the **original** text so
callers can highlight or rewrite the source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Token bodies
# ---------------------------------------------------------------------------
# An id / name body: starts alphanumeric, then alphanumerics plus a small set
# of separators commonly used in spec ids and agent role names
# (``001-feature``, ``qa_reviewer``, ``ABC-123``, ``v1.2``). A trailing
# separator is excluded so ``@coder.`` captures ``coder`` not ``coder.``.
_BODY = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"

# Task ref: a ``#`` that is NOT preceded by ``#`` or a word char, then a body.
# ``(?<![#\w])`` rejects ``##heading`` / ``###`` / ``foo#3``.
_TASK_RE = re.compile(rf"(?<![#\w])#({_BODY})")

# Mention: an ``@`` that is NOT preceded by a word char (rejects ``a@b.com``),
# then a body.
_MENTION_RE = re.compile(rf"(?<!\w)@({_BODY})")

# A URL run: scheme://… up to the next whitespace. Used only to mask spans so a
# ``#fragment`` inside a URL is not seen as a task ref.
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")

# Fenced (``` ... ```) and inline (` ... `) code spans. Fenced first (greedy on
# the fence) so an inner single backtick is not mistaken for inline code.
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


@dataclass(frozen=True)
class MentionRef:
    """A parsed reference extracted from text.

    Attributes:
        kind:     ``"task"`` (a ``#<id>`` ref) or ``"mention"`` (an ``@<name>``).
        value:    the token body without its sigil (e.g. ``"001"``, ``"coder"``).
        start:    inclusive offset of the sigil in the original text.
        end:      exclusive offset just past the token in the original text.
        resolved: ``None`` until resolution runs, then ``True``/``False``.
    """

    kind: str
    value: str
    start: int
    end: int
    resolved: bool | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain JSON-friendly dict."""
        return {
            "kind": self.kind,
            "value": self.value,
            "start": self.start,
            "end": self.end,
            "resolved": self.resolved,
        }


def _masked_spans(text: str) -> list[tuple[int, int]]:
    """Return [start, end) spans where sigils must be ignored.

    Covers fenced code, inline code, and URLs. Spans may overlap; callers only
    need a membership test, so we keep them as a simple list.
    """
    spans: list[tuple[int, int]] = []
    for rx in (_FENCED_RE, _INLINE_CODE_RE, _URL_RE):
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _in_masked(pos: int, spans: Iterable[tuple[int, int]]) -> bool:
    """True if ``pos`` falls inside any masked span."""
    return any(s <= pos < e for s, e in spans)


def parse_mentions(text: str) -> list[MentionRef]:
    """Extract ``#task`` and ``@mention`` refs from arbitrary text.

    Returns refs in order of appearance. Tokens inside code spans or URLs, and
    tokens that look like email addresses or Markdown headings, are skipped
    (see the module docstring for the full ruleset). ``resolved`` is ``None``
    on every returned ref; call :func:`resolve_mentions` to populate it.
    """
    if not text:
        return []

    spans = _masked_spans(text)
    refs: list[MentionRef] = []

    for kind, rx in (("task", _TASK_RE), ("mention", _MENTION_RE)):
        for m in rx.finditer(text):
            # The sigil is the char just before group(1).
            sigil_pos = m.start(1) - 1
            if _in_masked(sigil_pos, spans):
                continue
            refs.append(
                MentionRef(
                    kind=kind,
                    value=m.group(1),
                    start=m.start(),
                    end=m.end(),
                )
            )

    refs.sort(key=lambda r: r.start)
    return refs


def resolve_mentions(
    refs: list[MentionRef],
    known_task_ids: Iterable[str] | None = None,
    known_agent_names: Iterable[str] | None = None,
) -> list[MentionRef]:
    """Mark each ref resolved/unresolved against known sets (never throws).

    Resolution is case-insensitive for agent names (roles are lower-case by
    convention) and case-sensitive for task ids (spec ids preserve case, e.g.
    ``ABC-123``) with a case-insensitive fallback so ``#001`` matches ``001``.

    Args:
        refs:              refs from :func:`parse_mentions`.
        known_task_ids:    iterable of valid task / spec ids (or ``None`` →
                           every task ref is left unresolved).
        known_agent_names: iterable of valid agent / recipient names (or
                           ``None`` → every mention is left unresolved).

    Returns:
        New ``MentionRef`` objects with ``resolved`` set to ``True``/``False``.
    """
    task_ids = set(known_task_ids or ())
    task_ids_lower = {t.lower() for t in task_ids}
    agent_names_lower = {a.lower() for a in (known_agent_names or ())}

    resolved: list[MentionRef] = []
    for ref in refs:
        if ref.kind == "task":
            ok = ref.value in task_ids or ref.value.lower() in task_ids_lower
        else:
            ok = ref.value.lower() in agent_names_lower
        resolved.append(
            MentionRef(
                kind=ref.kind,
                value=ref.value,
                start=ref.start,
                end=ref.end,
                resolved=ok,
            )
        )
    return resolved


def leading_recipient(text: str) -> str | None:
    """Return the recipient from a leading ``@name`` directive, else ``None``.

    Only a mention that begins the (left-stripped) message addresses a
    recipient — ``@coder do X`` → ``"coder"``. A mid-sentence mention
    (``please ask @coder``) does NOT change routing, keeping ``enqueue``
    backward-compatible: text with no leading ``@`` resolves to ``None`` and the
    caller's default recipient is used unchanged.
    """
    if not text:
        return None
    stripped = text.lstrip()
    m = _MENTION_RE.match(stripped)
    if m and m.start() == 0:
        return m.group(1)
    return None
