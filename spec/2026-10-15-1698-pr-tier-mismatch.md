---
status: draft
issue: 1698
intent: intent/2026-10-15-1698-pr-tier-mismatch.md
---

# Spec: show the effective review tier and a tier mismatch on the PR

## Design

**D1. One rule, one function.** Move the verified-branch tier arithmetic out
of `merge_disposition` (`pr_endgame.py:576-587`) into
`_raise_to_signed(tier, signed) -> tuple[str, bool]`, which returns
`(effective, metadata_below_signed)`:

- `effective` starts as `tier`, or `"low"` when blank;
- `below` is `tier` non-blank and `raise_review_tier(tier, signed) != tier`;
- when `_describe_tier(effective) != "unrecognised"`,
  `effective = raise_review_tier(signed, effective)`.

`merge_disposition` calls it and keeps its warning and every hold exactly as
today. This is a pure refactor, so the PR body and the gate cannot drift apart.

**D2. Body note.** New `review_tier_note(spec_dir, tier, *, trusted) -> str`
in `pr_endgame.py`. It never raises and builds its text only from
`_describe_tier` labels and constants:

- It calls `resolve_contract(spec_dir, trusted)`. If the state is `verified`
  and the signed tier is usable (not `none`/`unrecognised`), it uses
  `_raise_to_signed`. The result is
  `Effective review tier: <label> (signed floor <label>).`, plus a
  warning block when `below` is true:
  `> **Possible tampering:** task metadata reviewTier <label> is below the
  signed tier <label>; the merge gate decides on <label>.`
- Otherwise (legacy, hold, no usable signed tier, any exception) the result
  is `Review tier: <label of tier, or "none"> (not checked against a signed
  contract).`

**D3. Endgame body.** In `run_pr_endgame` (`pr_endgame.py:1786`), after
`_pr_title_body`, append `\n\n` + `review_tier_note(...)` before the existing
path-floor note. `trusted` is the record the caller already looked up.

**D4. Merger body.** In `honest_pr_title_and_body` (`merger.py:280`), replace
the raw `tier_line` with `\n- ` + `review_tier_note(spec_dir, review_tier,
trusted=None)`. It is always present now (previously omitted when no tier).
With `trusted=None` the merger gets the "not checked" form, by design (intent
open question 1).

## Alternatives rejected

- **A GitHub PR comment or label for the mismatch.** It needs another `gh`
  call and more failure modes; the body is written once, in the same call.
- **Re-deriving the tier in the note.** It would duplicate `merge_disposition`'s
  rule and could drift; D1 shares it instead.
- **Trusted-record lookup in the merger.** The sweep runs in a thread and
  passes `trusted=None` on purpose; out of scope.
- **Echoing the raw metadata value.** It is agent-writable and would allow log
  and body injection; labels only.

## Risks

- Changed PR body text breaks tests that pin it: `test_merger.py:286`
  (`Review tier: medium`) needs the new wording.
- `test_merger` asserts the body never contains `clean`/`qa-passed`; the new
  text avoids both.
- `resolve_contract` imports lazily and can return `hold` when the loader is
  missing; the note then says "not checked", which is true.

## Verification

- `_raise_to_signed` cases: below, equal, above, blank, unrecognised.
- `merge_disposition` tests in `test_trusted_contract_merge.py` (21-28) stay
  green unchanged.
- `review_tier_note`: verified low-vs-signed-blocking carries "Possible
  tampering" with labels; verified equal has no warning; legacy says "not
  checked"; a crafted tier (`"low\n\n@evil"`) appears only as `unrecognised`.
- Endgame: `create_pr` receives a body containing the note.
- Merger: `Review tier: medium (not checked against a signed contract)`.
