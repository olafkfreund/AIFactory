---
status: draft
issue: 1698
spec: spec/2026-10-15-1698-pr-tier-mismatch.md
---

# Plan: show the effective review tier and a tier mismatch on the PR

Self-contained. You do not need to open the intent or the spec.

**Problem.** For a verified trusted task, `merge_disposition`
(`apps/web-server/server/services/pr_endgame.py:566-587`) takes the signed
`execution.review_tier` as the floor. When the agent-writable metadata
`reviewTier` is below it, the function only logs a warning. Neither PR body
says which tier the gate used: the endgame body (`pr_endgame.py:1786-1795`)
and the merger body (`services/merger.py:263-294`). The merger even echoes the
raw on-disk tier (`merger.py:280`). This change is visibility only; merge
decisions must not move.

## Decisions (carried from the approved spec)

- **D1** Extract the verified-branch tier arithmetic into
  `_raise_to_signed(tier, signed) -> (effective, below)`; `merge_disposition`
  calls it (pure refactor, same warning, same holds).
- **D2** `review_tier_note(spec_dir, tier, *, trusted) -> str`: verified and a
  usable signed tier gives `Effective review tier: <eff> (signed floor <signed>).`
  plus, when below, a `> **Possible tampering:** ...` line; otherwise
  `Review tier: <tier label or none> (not checked against a signed contract).`
  Only `_describe_tier` labels. Never raises.
- **D3** The endgame body appends the note before the path-floor note.
- **D4** The merger body replaces the raw tier line with the note
  (`trusted=None`, so always the "not checked" form); the line is always
  present now.

## Steps

### Step 1 — `_raise_to_signed` + `review_tier_note` in `pr_endgame.py`

File `apps/web-server/server/services/pr_endgame.py`.

1. Just above `def merge_disposition(` (line ~493) add:

```python
def _raise_to_signed(tier: str | None, signed: str) -> tuple[str, bool]:
    """``(tier decided on, metadata tier below signed)``; signed is the floor (#1672)."""
    from merge.merge_policy import raise_review_tier  # noqa: PLC0415

    blank = tier is None or not str(tier).strip()
    effective = "low" if blank else str(tier)
    below = not blank and raise_review_tier(tier, signed) != tier
    if _describe_tier(effective) != "unrecognised":
        effective = raise_review_tier(signed, effective)
    return effective, below


def review_tier_note(spec_dir: Path, tier: str | None, *, trusted: object) -> str:
    """The PR-body line for the review tier the merge gate uses (#1698).

    Labels only (``_describe_tier``): the metadata tier is agent-writable.
    Never raises.
    """
    try:
        from server.services.trusted_contract import (  # noqa: PLC0415
            resolve_contract,
        )

        state, contract = resolve_contract(spec_dir, trusted)
        execution = contract.get("execution") if state == "verified" else None
        signed = execution.get("review_tier") if isinstance(execution, dict) else None
        if isinstance(signed, str) and _describe_tier(signed) not in (
            "none",
            "unrecognised",
        ):
            effective, below = _raise_to_signed(tier, signed)
            note = (
                f"Effective review tier: {_describe_tier(effective)} "
                f"(signed floor {_describe_tier(signed)})."
            )
            if below:
                note += (
                    f"\n\n> **Possible tampering:** task metadata reviewTier "
                    f"{_describe_tier(tier)} is below the signed tier "
                    f"{_describe_tier(signed)}; the merge gate decides on "
                    f"{_describe_tier(effective)}."
                )
            return note
    except Exception:  # noqa: BLE001 - a body line must never break the endgame
        logger.debug("[pr-endgame] review tier note fell back", exc_info=True)
    label = _describe_tier(tier if tier is not None and str(tier).strip() else None)
    return f"Review tier: {label} (not checked against a signed contract)."
```

2. In `merge_disposition`, replace the verified-branch block from
   `if (\n tier is not None ...raise_review_tier(tier, signed) != tier\n):`
   through `effective = raise_review_tier(signed, effective)` (lines ~576-587)
   with:

```python
            effective, below = _raise_to_signed(tier, signed)
            if below:
                logger.warning(
                    "[pr-endgame] reviewTier %s below signed %s; "
                    "deciding on the stricter tier",
                    sanitize_log(_describe_tier(tier)),
                    sanitize_log(_describe_tier(signed)),
                )
```

   Keep `effective = tier if ... else "low"` above the `try` (non-verified
   paths use it). If `raise_review_tier` is no longer used in
   `merge_disposition`, drop it from that function's lazy import (keep
   `decide_merge`). Do not touch the holds before and after.

Tests, new file `apps/web-server/tests/test_pr_tier_note.py`, reusing
`_spec`, `_rec` and the autouse `_isolated_host` from
`test_trusted_contract_merge.py` (import them; re-declare the autouse fixture
via `from tests.test_trusted_contract_merge import _isolated_host  # noqa: F401`
or copy it, whichever the import layout allows):

- `_raise_to_signed`: `("low","blocking") -> ("blocking", True)`,
  `("blocking","auto") -> ("blocking", False)`, `(None,"auto") -> ("auto", False)`,
  `("auto","auto") -> ("auto", False)`, `("bogus","auto") -> ("bogus", True or False as raise_review_tier gives; assert effective == "bogus")`.
- verified `_spec(review_tier="blocking")`, `tier="low"`: note contains
  `Effective review tier: blocking` and `Possible tampering` and
  `reviewTier low is below the signed tier blocking`.
- verified `_spec(review_tier="auto")`, tier `"auto"`: no `Possible tampering`.
- legacy (`trusted=None`, contract file removed): `Review tier: low (not checked`.
- crafted tier `"low\n\n@evil"` on legacy: note contains `unrecognised` and not `@evil`.

Check: `cd apps/web-server && python -m pytest tests/test_pr_tier_note.py tests/test_trusted_contract_merge.py tests/test_pr_endgame_review_tier.py tests/test_pr_endgame_merge_gate.py -q`
Traps: `merge_disposition` behaviour must be byte-for-byte the same; tests
21-28 in `test_trusted_contract_merge.py` pin it. `raise_review_tier` takes
`(tier, floor)` and returns the stricter; check its signature in
`apps/backend/merge/merge_policy.py` before relying on argument order.

### Step 2 — use the note in both PR bodies

- `pr_endgame.py`, `run_pr_endgame`, right after
  `title, body = _pr_title_body(spec_dir, spec_id)` (line ~1786):
  `body = f"{body}\n\n{review_tier_note(spec_dir, review_tier, trusted=trusted)}"`.
  The path-floor block below stays as is.
- `merger.py`, `honest_pr_title_and_body` (line 280): replace
  `tier_line = f"\n- Review tier: {review_tier}" if review_tier else ""` with
  `tier_line = f"\n- {pe.review_tier_note(spec_dir, review_tier, trusted=None)}"`.

Tests:
- `apps/web-server/tests/test_merger.py:286`: change to
  `assert "Review tier: medium (not checked against a signed contract)" in body`.
  Add one assertion in a `None` tier test: `"Review tier: none (not checked" in body`.
- New test in `test_pr_tier_note.py`: run `run_pr_endgame` with
  `background=False`, `auto_merge=False`, `reviewer="copilot"`,
  monkeypatched `create_pr` capturing `body`, `request_copilot_review`,
  `watch_and_finish` (pattern of
  `test_1695_endgame_arms_the_gate_only_for_non_legacy`), verified
  `_spec(review_tier="blocking")`, `review_tier="low"`: the captured body
  contains `Possible tampering`.

Check: `cd apps/web-server && python -m pytest tests/test_merger.py tests/test_pr_tier_note.py tests/test_pr_endgame.py -q`
Traps: the merger body must not contain `clean` or `qa-passed`
(`test_honest_body_never_claims_qa_passed`); the note text has neither.
Do not change `_pr_title_body` (other tests pin it).

### Step 3 — CHANGELOG

`CHANGELOG.md`, `## [Unreleased]` → `### Changed`: PR bodies show the
effective review tier and flag a metadata tier below the signed tier as
possible tampering (#1698); merger body no longer echoes the raw tier.

## Tests (summary)

Regression proof: the endgame-body test and the tampering note test fail on
`dev` (no note). Also `ruff check`, `ruff format --check`, the repo's
pre-commit cq ratchet (ruff/mypy strict, runs on commit).

## Rollback

Revert the PR. Body text only; no stored state.
