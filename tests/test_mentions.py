"""
Tests for ``#task`` / ``@agent`` mention parsing & resolution (#273).

Covers:
- Parsing correctness for ``#task`` and ``@mention`` tokens (+ offsets).
- False-match guards: email addresses, fenced + inline code, URL fragments,
  ``##`` Markdown headings, mid-word ``#``/``@``.
- Resolution against a known set (resolved / unresolved, no throw on unknown).
- Inbox enqueue integration: leading ``@coder`` routes to recipient ``coder``;
  ``#task`` refs are attached to the stored message; backward-compat (no ``@``
  → default recipient unchanged, payload shape intact).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The backend (parser) and web-server (inbox writer) live in separate apps.
_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

# Load the pure parser module directly to avoid importing the agents package
# (and its SDK deps), mirroring the inbox test's loading strategy.
_parser_path = _BACKEND / "agents" / "mentions.py"
_spec = importlib.util.spec_from_file_location("aifactory_mentions_test", _parser_path)
mentions = importlib.util.module_from_spec(_spec)
# Register before exec so the @dataclass body can resolve the module's globals.
sys.modules["aifactory_mentions_test"] = mentions
_spec.loader.exec_module(mentions)

from server.services import inbox_service  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Parsing correctness
# ---------------------------------------------------------------------------


def _kinds(text: str) -> list[tuple[str, str]]:
    return [(r.kind, r.value) for r in mentions.parse_mentions(text)]


def test_parses_task_and_mention_tokens():
    refs = mentions.parse_mentions("@coder please look at #001-feature")
    assert [(r.kind, r.value) for r in refs] == [
        ("mention", "coder"),
        ("task", "001-feature"),
    ]


def test_offsets_point_into_original_text():
    text = "hi @coder see #42"
    refs = mentions.parse_mentions(text)
    by_kind = {r.kind: r for r in refs}
    assert text[by_kind["mention"].start : by_kind["mention"].end] == "@coder"
    assert text[by_kind["task"].start : by_kind["task"].end] == "#42"


def test_refs_returned_in_order_of_appearance():
    refs = mentions.parse_mentions("#001 then @coder then #002")
    assert [r.value for r in refs] == ["001", "coder", "002"]


def test_numeric_and_dotted_and_underscore_ids():
    assert _kinds("#001 #ABC-123 #v1.2 @qa_reviewer") == [
        ("task", "001"),
        ("task", "ABC-123"),
        ("task", "v1.2"),
        ("mention", "qa_reviewer"),
    ]


def test_empty_and_none_text():
    assert mentions.parse_mentions("") == []
    assert mentions.parse_mentions(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# False-match guards
# ---------------------------------------------------------------------------


def test_email_address_is_not_a_mention():
    assert _kinds("contact a@b.com or user@host.org") == []


def test_mention_after_word_char_not_matched():
    # foo@bar must not yield @bar (no preceding-word-char mentions).
    assert _kinds("foo@bar") == []


def test_inline_code_is_ignored():
    assert _kinds("use `@coder` and `#001` as examples") == []


def test_fenced_code_block_is_ignored():
    text = "before\n```\n@coder do #001\n```\nafter @planner"
    assert _kinds(text) == [("mention", "planner")]


def test_url_fragment_is_ignored():
    text = "see https://example.com/page#section for details"
    assert _kinds(text) == []


def test_url_fragment_ignored_but_following_token_kept():
    text = "https://x.io/p#frag then ping @coder"
    assert _kinds(text) == [("mention", "coder")]


def test_markdown_headings_are_ignored():
    assert _kinds("## Heading\n### Sub\n#realtask") == [("task", "realtask")]


def test_hash_after_word_char_not_matched():
    # issue#3 / foo#bar must not match as a task ref.
    assert _kinds("issue#3 and foo#bar") == []


def test_trailing_punctuation_excluded():
    refs = mentions.parse_mentions("ping @coder, and close #001.")
    assert [(r.kind, r.value) for r in refs] == [
        ("mention", "coder"),
        ("task", "001"),
    ]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolution_marks_known_and_unknown():
    refs = mentions.parse_mentions("@coder @ghost #001 #999")
    resolved = mentions.resolve_mentions(
        refs,
        known_task_ids={"001", "002"},
        known_agent_names={"coder", "planner"},
    )
    got = {(r.kind, r.value): r.resolved for r in resolved}
    assert got == {
        ("mention", "coder"): True,
        ("mention", "ghost"): False,
        ("task", "001"): True,
        ("task", "999"): False,
    }


def test_resolution_never_throws_on_unknown():
    refs = mentions.parse_mentions("@nobody #nothere")
    # No known sets supplied → all unresolved, no exception.
    resolved = mentions.resolve_mentions(refs)
    assert all(r.resolved is False for r in resolved)


def test_agent_name_resolution_case_insensitive():
    refs = mentions.parse_mentions("@Coder")
    resolved = mentions.resolve_mentions(refs, known_agent_names={"coder"})
    assert resolved[0].resolved is True


def test_task_id_resolution_exact_then_case_insensitive():
    refs = mentions.parse_mentions("#ABC-123 #abc-123")
    resolved = mentions.resolve_mentions(refs, known_task_ids={"ABC-123"})
    # First matches exactly; second matches via case-insensitive fallback.
    assert [r.resolved for r in resolved] == [True, True]


def test_resolution_returns_new_objects_without_mutating_input():
    refs = mentions.parse_mentions("@coder")
    resolved = mentions.resolve_mentions(refs, known_agent_names={"coder"})
    assert refs[0].resolved is None  # original untouched
    assert resolved[0].resolved is True


# ---------------------------------------------------------------------------
# leading_recipient helper
# ---------------------------------------------------------------------------


def test_leading_recipient_detected():
    assert mentions.leading_recipient("@coder do the thing") == "coder"
    assert mentions.leading_recipient("   @planner  plan it") == "planner"


def test_leading_recipient_none_when_mid_sentence():
    assert mentions.leading_recipient("please ask @coder") is None
    assert mentions.leading_recipient("no mention here") is None
    assert mentions.leading_recipient("") is None
    # An email at the start is not a recipient directive.
    assert mentions.leading_recipient("a@b.com hello") is None


# ---------------------------------------------------------------------------
# Inbox integration (#264 enqueue seam)
# ---------------------------------------------------------------------------


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    d = tmp_path / "001-feature"
    d.mkdir(parents=True)
    return d


def test_enqueue_leading_at_routes_to_recipient(spec_dir: Path):
    result = inbox_service.enqueue(spec_dir, text="@coder implement the parser")
    assert result["recipient"] == "coder"
    # The message landed in the coder inbox, not the default agent inbox.
    coder = inbox_service.list_messages(spec_dir, recipient="coder")
    assert len(coder) == 1
    assert coder[0]["text"] == "@coder implement the parser"
    assert inbox_service.list_messages(spec_dir, recipient="agent") == []


def test_enqueue_attaches_task_refs(spec_dir: Path):
    inbox_service.enqueue(spec_dir, text="@coder fix #001-feature and check #002")
    msgs = inbox_service.list_messages(spec_dir, recipient="coder")
    assert len(msgs) == 1
    refs = msgs[0]["taskRefs"]
    assert [r["value"] for r in refs] == ["001-feature", "002"]
    assert all({"value", "start", "end"} <= set(r) for r in refs)


def test_enqueue_explicit_recipient_wins_over_mention(spec_dir: Path):
    # An explicit recipient must not be overridden by a leading @mention.
    result = inbox_service.enqueue(spec_dir, text="@coder do X", recipient="planner")
    assert result["recipient"] == "planner"
    assert inbox_service.list_messages(spec_dir, recipient="coder") == []
    assert len(inbox_service.list_messages(spec_dir, recipient="planner")) == 1


def test_enqueue_no_mention_uses_default_recipient(spec_dir: Path):
    # Backward-compat: no leading @ → default 'agent' recipient, no taskRefs key.
    result = inbox_service.enqueue(spec_dir, text="just a plain instruction")
    assert result["recipient"] == "agent"
    msgs = inbox_service.list_messages(spec_dir, recipient="agent")
    assert len(msgs) == 1
    assert "taskRefs" not in msgs[0]


def test_enqueue_mid_sentence_mention_does_not_route(spec_dir: Path):
    # A mention that is not at the start keeps the default recipient, but its
    # #task refs are still attached.
    result = inbox_service.enqueue(spec_dir, text="please tell @coder about #001")
    assert result["recipient"] == "agent"
    msgs = inbox_service.list_messages(spec_dir, recipient="agent")
    assert [r["value"] for r in msgs[0]["taskRefs"]] == ["001"]
