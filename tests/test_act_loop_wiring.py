"""Wiring tests for #474/#476: guardrail halt signal + mutation ledger in handoff."""

from __future__ import annotations

import json
from pathlib import Path

from agents.act_loop_hooks import _write_halt, read_halt_reason
from agents.mutation_ledger import MutationLedger
from pfactory.tfactory_client import build_handoff_payload


class _Classification:
    handoff = "testing"
    types = ("software",)
    priority = "p1"


def test_halt_reason_roundtrip(tmp_path: Path):
    assert read_halt_reason(tmp_path) is None  # nothing halted
    _write_halt(tmp_path, "Edit failed 8× — halting (no progress)")
    assert read_halt_reason(tmp_path) == "Edit failed 8× — halting (no progress)"
    # persisted as the expected file the loops + completion event read
    assert (tmp_path / "guardrail_halt.json").exists()
    assert json.loads((tmp_path / "guardrail_halt.json").read_text())["halt_reason"]


def test_handoff_payload_embeds_mutation_ledger(tmp_path: Path):
    led = MutationLedger(tmp_path)
    led.record(tool="Write", target="api/limit.py", ok=True, tool_use_id="t1")
    led.record(tool="Bash", target="pytest", ok=True, tool_use_id="t2")

    payload = build_handoff_payload(
        "001-x", {"title": "t"}, _Classification(), {}, spec_dir=tmp_path
    )
    assert "mutations" in payload
    assert [m["tool"] for m in payload["mutations"]] == ["Write", "Bash"]
    assert payload["mutations"][0]["target"] == "api/limit.py"


def test_handoff_payload_omits_mutations_when_none(tmp_path: Path):
    # No ledger file → no "mutations" key (backward-compatible).
    payload = build_handoff_payload(
        "001-x", {"title": "t"}, _Classification(), {}, spec_dir=tmp_path
    )
    assert "mutations" not in payload


def test_handoff_payload_unchanged_without_spec_dir():
    # Existing callers that don't pass spec_dir are byte-for-byte unaffected.
    payload = build_handoff_payload("001-x", {"title": "t"}, _Classification(), {})
    assert "mutations" not in payload
    assert payload["spec_id"] == "001-x"
