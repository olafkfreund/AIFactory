---
status: approved
issue: 2255
intent: intent/2026-11-26-2255-tf-verdict-project.md
---

# Spec: record TFactory's project id at handoff and scope the verdict lookup with it

## Design

All the code changes are in `apps/backend/pfactory/tfactory_client.py`.

### 1. `_httpx_poster` (~line 968) and `send_handoff` (~line 1017)

**The poster.** TFactory puts `workspace_project_id` last in its JSON, after `**result`, so it can
land past the 2000-character `body` cutoff. When `resp.is_success`, `_httpx_poster` parses the
**full** response with `resp.json()`. That parse is best-effort, and any exception is ignored. The
poster then adds `"workspace_project_id": <value>` to its return dict when the value is a string.
Only that one field is kept. The body is never stored or logged beyond today's truncated `body`.

**`send_handoff`.** When `ok` is true and `result.get("workspace_project_id")` matches
`[A-Za-z0-9._-]{1,128}`, the returned dict gains `"tfactory_project_id": <value>`. An injected
`poster` that returns no such key adds nothing.

**The marker write.** `maybe_auto_handoff_tfactory` (~line 1375) writes the marker atomically: to
`tfactory_handoff.json.tmp`, then `os.replace` over the real file. It still writes every result,
as it does today. A failed retry therefore leaves no recorded id, and the verdict lookup falls
back to the bare lookup. That fallback is still fail-closed: when spec ids collide, TFactory
answers 409, which gives `None`. The gate is not weakened; only the collision fix is lost for that
one task.

### 2. `_recorded_tfactory_project(spec_dir) -> str | None`

This new helper reads `spec_dir / "tfactory_handoff.json"` and returns `tfactory_project_id` when
it is present, the record has `sent` set to true, and the id matches the pattern. Otherwise it
returns `None`. It never raises.

### 3. `tfactory_verdict` (~line 1255)

```python
recorded = _recorded_tfactory_project(Path(spec_dir))
project_id = recorded or os.environ.get("TFACTORY_PROJECT_ID") or _aifactory_project_name(Path(spec_dir))
...
base = f"{base_url}/api/tfactory/tasks/{quote(spec_id, safe='')}"
qs = f"?project_id={quote(recorded, safe='')}" if recorded else ""
code, task = get(f"{base}{qs}", headers)
...  # the existing checks, with task.get("project_id") == project_id
code, doc = get(f"{base}/verdicts.json{qs}", headers)
```

- **Only a recorded id scopes the lookup.** Without a record, the env and derived-name paths behave
  exactly as today, with bare URLs and the same equality check. The env override's meaning is
  unchanged, and the existing tests, which set `TFACTORY_PROJECT_ID=demo` and stub bare URLs, pass
  unchanged.
- **With a recorded id**, a scoped TFactory response returns that same canonical id. TFactory's
  ingest resolves the project to the registered key and returns it as `workspace_project_id`, and a
  scoped `get_task` returns the workspace directory name, which is that key. The equality check
  holds.
- **No bare retry** after a scoped failure. A failure gives `None`.
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

- **Tasks handed off before this release**, or whose last handoff failed, have no recorded id. They
  keep today's lookup, so a collision still gives `None`. Re-handing them off records the id.
- **The marker sits in the spec directory, which the build agent can write.** A forged
  `tfactory_project_id` could point the lookup at another project with the same spec id. The gate
  still requires that project's task to have `source_sha == head`, the PR head SHA, and a build
  commit is not shared across projects. This is accepted for this fix. Moving the binding to a
  store outside the agent's reach is a follow-up.
- **Pre-existing:** the task GET and the `verdicts.json` GET are separate reads. That is unchanged.
- **Prod:** a normal AIFactory release (3.21.17).

## Verification

Tests in `apps/backend/pfactory/test_tfactory_verdict.py` and the handoff tests:

| Case | Expected |
|---|---|
| `_httpx_poster` with a success JSON response longer than 2000 chars, ending with `workspace_project_id` | the return dict carries `workspace_project_id`; `body` is still truncated |
| `send_handoff` with a poster result carrying `workspace_project_id: "2b2e…"` | the result carries `tfactory_project_id` |
| non-JSON body, missing key, or a value outside the pattern | no field added |
| `tfactory_verdict` with a recorded id | requests `TASK?project_id=2b2e…` and `TASK/verdicts.json?project_id=2b2e…`; returns `pass` / `fail` as before |
| recorded id, but the response `project_id` differs | `None` |
| no record, no env | the existing bare URLs; the existing tests pass unchanged |
| env `TFACTORY_PROJECT_ID` only | unchanged: bare URLs, equality with the env value |
| a record with `sent: false` | ignored (legacy lookup) |
| the marker write | atomic (`.tmp` then `os.replace`) |
| malformed or unreadable `tfactory_handoff.json` | legacy behaviour; never raises |

Then:

- The `pfactory` tests and the endgame tests that use `tfactory_verdict` keep passing.
- CI is green.
- Live: release 3.21.17. E2E P1 records `tfactory_project_id`, the verdict lookup returns 200, the PR
  merges (S4) and S5 runs.
