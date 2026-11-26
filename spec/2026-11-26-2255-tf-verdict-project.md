---
status: draft
issue: 2255
intent: intent/2026-11-26-2255-tf-verdict-project.md
---

# Spec: record TFactory's project id at handoff and scope the verdict lookup with it

## Design

All the code changes are in `apps/backend/pfactory/tfactory_client.py`.

### 1. `send_handoff` (~line 1017)

After `result = await poster(url, payload, headers)`, when `ok` is true, parse `result.get("body")`
as JSON. This is best-effort: a parse failure or a non-dict body is ignored. If the parsed body has a
`workspace_project_id` that is a string matching `[A-Za-z0-9._-]{1,128}`, the returned dict gains
`"tfactory_project_id": <that value>`.

`maybe_auto_handoff_tfactory` (~line 1375) already writes the result to `tfactory_handoff.json`,
so the id is recorded with no further change. The body text itself is never stored or logged.

### 2. `_recorded_tfactory_project(spec_dir) -> str | None`

This new helper reads `spec_dir / "tfactory_handoff.json"`. It returns `tfactory_project_id` when
that field is present and matches the same pattern. Otherwise it returns `None`. It never raises.

### 3. `tfactory_verdict` (~line 1255)

```python
scoped = _recorded_tfactory_project(Path(spec_dir)) or os.environ.get("TFACTORY_PROJECT_ID")
project_id = scoped or _aifactory_project_name(Path(spec_dir))
...
base = f"{base_url}/api/tfactory/tasks/{quote(spec_id, safe='')}"
qs = f"?project_id={quote(scoped, safe='')}" if scoped else ""
code, task = get(f"{base}{qs}", headers)
...  # the existing checks, with task.get("project_id") == project_id
code, doc = get(f"{base}/verdicts.json{qs}", headers)
```

- **Precedence:** the recorded id comes first. Next is the explicit `TFACTORY_PROJECT_ID`, which is
  already a TFactory id by its name and meaning, so it scopes too. Last is the derived AIFactory
  name, with today's bare lookup.
- **The equality check stays.** When the lookup is scoped, TFactory returns the resolved id, which
  equals the recorded `workspace_project_id`.
- **No fallback** to a bare lookup when the scoped one fails. A failure is `None`, which is fail
  closed.
- **Everything else is unchanged:** the `source_sha` and `triaged` checks, the verdict-kind rule,
  the vote summary, and "never raises".

## Alternatives rejected

- **TFactory resolves the directory-name alias:** this relies on a naming convention. The owner
  chose this design instead.
- **Map the AIFactory name to the TFactory name** by stripping `olafkfreund-`: this is fragile and
  owner-specific.
- **Fall back to a bare lookup on a 404 or 409:** that hides a broken record. The 409 is the bug
  being fixed.

## Risks

- **Tasks handed off before the release** have no recorded id. They keep today's behaviour and still
  get the 409 when spec ids collide. Re-handing them off records the id.
- **Contract assumption:** that `workspace_project_id` equals the id TFactory resolves and returns
  on a scoped lookup. A live check on prod showed both `?project_id=<UUID>` and `?project_id=<name>`
  returning the UUID. A unit test pins AIFactory's side, and the live E2E run proves the whole path.
- **Prod:** a normal AIFactory release (3.21.17).

## Verification

Tests in `apps/backend/pfactory/test_tfactory_verdict.py` and the handoff tests:

| Case | Expected |
|---|---|
| `send_handoff` body `{"workspace_project_id": "2b2e…"}` | result carries `tfactory_project_id` |
| non-JSON body, missing key, or a value outside the pattern | no field added |
| `tfactory_verdict` with a recorded id | requests `TASK?project_id=2b2e…` and `TASK/verdicts.json?project_id=2b2e…`; returns `pass` / `fail` as before |
| recorded id, but the response `project_id` differs | `None` |
| no record, no env | the existing bare URLs; the existing tests pass unchanged |
| env `TFACTORY_PROJECT_ID` only | scoped with the env value |
| malformed or unreadable `tfactory_handoff.json` | legacy behaviour; never raises |

Then:

- The `pfactory` tests and the endgame tests that use `tfactory_verdict` keep passing.
- CI is green.
- Live: release 3.21.17. E2E P1 records `tfactory_project_id`, the verdict lookup returns 200, the PR
  merges (S4) and S5 runs.
