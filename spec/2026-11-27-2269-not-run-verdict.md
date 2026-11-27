---
status: draft
issue: 2269
intent: intent/2026-11-27-2269-not-run-verdict.md
---

# Spec: a `not_run` TFactory verdict no longer holds the merge gate forever

## Design

In `apps/backend/pfactory/tfactory_client.py` `tfactory_verdict` (~1380–1388), after reading
`kinds`:

```python
kinds = [k for k in kinds if k != "not_run"]   # TFactory: no evidence, neither pass nor fail
if not kinds or any(k not in ("accept", "flag", "reject") for k in kinds):
    return None
```

The rest is unchanged:
- `"fail"` if any `reject`, else `"pass"`;
- the vote summary is still filled.

The docstring gains one line: "`not_run` rows (no evidence) are ignored."

## Alternatives rejected

- **Treat `not_run` as `reject`:** that fails correct builds whenever a generated test is broken,
  which is TFactory's defect, not the code's. TFactory's own VAL already excludes these rows.
- **Treat `not_run` as `flag` (pass):** wrong when it is the only evidence. This design gives
  `None` in that case.

## Risks

- **A build passes on fewer tests:** each accepted row is still a measured pass, a `reject` still
  fails, and an all-`not_run` run still holds.

## Verification

| Verdicts | Result |
|---|---|
| `accept`, `not_run` | `pass` |
| `accept`, `reject`, `not_run` | `fail` |
| `not_run` only | `None` |
| `flag`, `not_run` | `pass` |
| `accept`, `maybe` | `None` (unchanged) |

The existing `test_tfactory_verdict.py` and the merge-gate tests pass, the ratchets are clean,
and CI is green.
