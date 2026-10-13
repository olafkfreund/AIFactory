---
status: approved
issue: 1750
spec: spec/2026-10-11-1750-tfactory-block-trusted.md
---

# Plan: the TFactory test profile must come from the trusted contract

Self-contained summary of the approved decisions. You do not need to open the
intent or the spec.

**Problem.** `start_task` (web server) routes TFactory-labelled specs to
TFactory. It reads the `tfactory` test profile with `load_tfactory_block`, which
reads `implementation_plan.json` unchecked. An agent can edit that file, so a
trusted-plan task can hand TFactory a forged profile.

**Approved decisions** (intent decisions 2026-10-11, spec decisions 2026-10-13):

1. Read the profile from the web server's trusted-contract verdict,
   `resolve_contract(spec_dir, await lookup(spec_dir))`
   (`apps/web-server/server/services/trusted_contract.py:67`). No new helper.
   Do NOT use `core.contract_trust.trusted_contract` (it reads a pod env var the
   web server never has) and do NOT use `handoff_contract` (it returns `None` for
   legacy and would need a second call).
2. The three states and what is sent as `tfactory`:

   | State | When | `tfactory` sent |
   | --- | --- | --- |
   | `verified` | isolated host, signed record verifies, disk copy matches | the record's signed block |
   | `hold` | `LOOKUP_FAILED`, record does not verify, non-isolated host with a record, trusted trace with no record, any exception | `{}`; the handoff is **still sent** (do not skip it) |
   | `legacy` | no record and no trusted trace | the block from `load_task_contract` (`context/task_contract.json` first, then `implementation_plan.json`), or `{}` |

3. On hold, log exactly one `logger.warning` naming the task and saying the
   declared profile was withheld. It names the state only, never the contract
   content.
4. Accepted: if the store is unreachable on an isolated host, even correctly
   signed tasks send `{}` until it is back (same as the merge gate and
   `handoff_to_tfactory`).
5. Accepted: hosts that cannot verify (not kubejob with `DATABASE_URL`) send
   `{}` for trusted-plan tasks. Legacy tasks are unaffected. Do NOT change
   `host_isolated` here; file a follow-up issue instead (step 7).
6. Delete `load_tfactory_block`, its exports and its three pinning tests. It has
   no other caller.
7. Nothing else in the route changes: `build_handoff_payload`, `send_handoff`,
   the `TFACTORY_HANDOFF.md` marker, the response, and the
   `except (json.JSONDecodeError, OSError, ImportError)` stay as they are.
   (`lookup` never raises; `resolve_contract` catches everything and holds, so
   the narrow except cannot turn a trust failure into a coder run.)
8. The branch is already rebased on dev after PR #1751 (decision met). PR #1752
   also edits `execution.py`; if it lands first, rebase and resolve the import
   line.

**Environment.** Worktree `/mnt/code/Source-home/GitHub/AIFactory-1750`, branch
`fix/1750-tfactory-block-trusted`. Python is the shared backend venv:
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`. Run every
command from the worktree root.

**Repo traps (all steps).**

- Run the ruff/mypy ratchet with the files **staged**:
  `git add <files> && $V/python scripts/cq_ratchet.py --staged`.
- `standards/coding-standards.md` is vendored. Never edit it.
- Root tests need `-c tests/pytest.ini` (it sets `asyncio_mode = auto`).
  Web-server tests run from the root with the same ini, or as CI does with
  `-o asyncio_mode=auto`. Running them from `apps/web-server` without it fails
  the async tests.
- Do not edit the intent or spec files. Name the plan step in each commit
  message, e.g. `fix(tfactory): step 1 ... (#1750)`.

## Steps

1. `apps/web-server/server/routes/execution.py` — switch the source of `_tf`.
   - Line 32: change
     `from server.services.trusted_contract import handoff_contract` to
     `from server.services.trusted_contract import handoff_contract, resolve_contract`.
     (`lookup` is already imported at lines 33-37; `logger` is the local one set
     at line 297.)
   - Lines 349-353 (in-function import): remove the `load_tfactory_block,`
     line (351), leaving `build_handoff_payload` and `send_handoff`.
   - Lines 364-366: replace the comment and `_tf = load_tfactory_block(spec_dir)`
     with:

     ```python
                 # RFC-0002 / #1750: the declared test profile comes only from the
                 # trusted verdict (signed record, or legacy files); hold sends {}.
                 _state, _contract = resolve_contract(spec_dir, await lookup(spec_dir))
                 _tf = _contract.get("tfactory") if isinstance(_contract, dict) else None
                 _tf = _tf if isinstance(_tf, dict) else {}
                 if _state == "hold":
                     logger.warning(
                         "[StartTask] %s trusted contract on hold; declared "
                         "TFactory profile withheld",
                         sanitize_log(task_id),
                     )
     ```

     Leave `tfactory=_tf` in the `build_handoff_payload` call unchanged.
   → verify by `grep -n "load_tfactory_block\|resolve_contract" apps/web-server/server/routes/execution.py`
   (only the import and the new call match). Commit together with step 2, or the
   import in step 1's file still points at the deleted name — keep steps 1 and 2
   in one commit.
   Traps: the log line must not include `_contract` or `_tf`; use
   `sanitize_log` on the task id (CodeQL log injection). Ratchet staged.

2. Delete `load_tfactory_block` (decision 6).
   - `apps/backend/pfactory/tfactory_client.py`: line 40 (`"load_tfactory_block",`
     in `__all__`) and lines 69-85 (the function) plus one of the two blank
     lines that follow. Keep the `json` and `Path` imports; `load_task_contract`
     still uses them.
   - `apps/backend/pfactory/__init__.py`: line 25 (import) and line 43
     (`__all__` entry).
   - `tests/test_pfactory_tfactory_client.py`: line 15 (import) and lines 69-87
     (the three `test_load_tfactory_block_*` tests) plus the blank lines between
     them. Keep `import json`; lines 97+ still use it.
   → verify by `grep -rn load_tfactory_block apps tests` (no output) and
   `$V/python -m pytest -c tests/pytest.ini tests/test_pfactory_tfactory_client.py -q`
   (passes).
   Traps: ratchet staged. Commit steps 1+2 as one commit:
   `fix(tfactory): steps 1-2 read the TFactory profile from the trusted verdict (#1750)`.

3. `tests/test_pfactory_routing.py` — stub the lookup and pin hold/legacy.
   - In `test_start_task_routes_tfactory_child_away_from_coder` (line 119) and
     `test_start_task_tfactory_sends_when_configured` (line 141), add after the
     `load_projects` patch:

     ```python
         async def _no_record(_spec_dir):
             return None

         monkeypatch.setattr(exec_mod, "lookup", _no_record)
     ```

     (Without it `lookup` tries the DB, returns `LOOKUP_FAILED`, and the test
     depends on the environment.) Hoist `_no_record` to a module-level helper
     if you prefer; reuse it in the new tests.
   - Add `test_start_task_tfactory_hold_withholds_edited_profile(tmp_path, monkeypatch, caplog)`
     using the existing `_write_spec` and `fake_poster` pattern (TFACTORY_BASE_URL
     set, `tfactory_client._httpx_poster` patched). Spec dir
     `project/.aifactory/specs/001-tests`: write
     `context/task_contract.json` = `{"approval": {"by": "pfactory"}, "tfactory": {"lanes": ["unit"]}}`
     and `implementation_plan.json` = `{"tfactory": {"lanes": ["forged"]}}`.
     `lookup` returns `None`. Assert `result["routed_to"] == "tfactory"`,
     `result["transport"]["sent"] is True`, `sent["payload"]["tfactory"] == {}`,
     a WARNING record containing `withheld`, and that `"forged"` appears in no
     log message.
   - Add `test_start_task_tfactory_legacy_reads_task_contract(tmp_path, monkeypatch)`:
     same setup but `context/task_contract.json` =
     `{"contract_version": 2, "tfactory": {"lanes": ["unit"]}}` (no `approval`)
     and no `implementation_plan.json`. `lookup` returns `None`. Assert
     `sent["payload"]["tfactory"] == {"lanes": ["unit"]}`.
   → verify by
   `$V/python -m pytest -c tests/pytest.ini tests/test_pfactory_routing.py -q`
   (all pass). Confirm the new tests pin the bug against the pre-fix code
   (BASE = the commit before steps 1-2, from `git log --oneline`):
   `git checkout BASE -- apps/web-server/server/routes/execution.py apps/backend/pfactory tests/test_pfactory_tfactory_client.py && $V/python -m pytest -c tests/pytest.ini tests/test_pfactory_routing.py -q -k "hold or legacy"; git checkout HEAD -- apps/web-server/server/routes/execution.py apps/backend/pfactory tests/test_pfactory_tfactory_client.py`
   Expected: both new tests fail (hold receives `{"lanes": ["forged"]}`), then
   `git status` is clean again for those paths.
   Traps: the test file inserts `apps/web-server` and `apps/backend` on
   `sys.path` itself (lines 16-19); do not add another path hack. Ratchet staged.

4. `apps/web-server/tests/test_trusted_contract_merge.py` — verified and
   non-isolated cases, inserted after `test_20_handoff_uses_the_record_contract`
   (lines 271-293). The file's autouse fixture `_isolated_host` (lines 45-54)
   already makes the host isolated and sets the signing key; `_spec` (line 96)
   ingests a signed plan whose `tfactory` is `{"lanes": ["functional"]}`;
   `_rec` (line 120) builds a `TrustedRecord`.
   Shared setup (write a small local helper, e.g. `_start_tfactory(tmp_path, monkeypatch, record)`
   returning the captured payload):
   - `spec, signed = _spec(tmp_path)`.
   - Make it TFactory-routed: load `spec / "requirements.json"`, set
     `req["githubIssue"] = {"number": 1, "labels": ["pfactory", "handoff:tfactory", "type:testing"]}`
     (keep the existing keys, including `provenance`), write it back.
   - Forge the build-rewritten file: load `spec / "implementation_plan.json"`,
     set `["tfactory"] = {"lanes": ["forged"]}`, write it back. Do NOT touch
     `context/task_contract.json` (the record must still match it).
   - `monkeypatch.setattr("server.routes.execution.load_projects", lambda: {"p": {"path": str(tmp_path)}})`;
     patch `server.routes.execution.lookup` with an async fake returning `record`;
     `monkeypatch.setenv("TFACTORY_BASE_URL", "https://tf.example")`; patch
     `tc._httpx_poster` with an async fake that stores the payload and returns
     `{"status": 202, "ok": True, "body": "queued"}`.
   - Call `await start_task("p:001-x", StartTaskRequest(), None)` (import both
     from `server.routes.execution` inside the test or at the bottom import
     block with `# noqa: E402`).
   Tests (both `async def`):
   - `test_30_start_task_hands_off_the_signed_profile`: record `_rec(signed)`.
     Expect `payload["tfactory"] == signed["tfactory"]` and `!= {"lanes": ["forged"]}`.
   - `test_31_start_task_non_isolated_host_sends_empty_profile`:
     `monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")`, record
     `_rec(signed)`. Expect `payload["tfactory"] == {}` and the route still
     returned `routed_to == "tfactory"` (pins decisions 2 and 5).
   → verify by
   `$V/python -m pytest -c tests/pytest.ini apps/web-server/tests/test_trusted_contract_merge.py -q`
   (43 passed: 41 existing + 2).
   Traps: number new tests after the file's existing scheme (`test_29` is the
   last numbered one). Ratchet staged. Commit steps 3+4:
   `test(tfactory): steps 3-4 pin verified/hold/legacy TFactory profile (#1750)`.

5. `CHANGELOG.md` — under `## [Unreleased]` → `### Security` (line 3), add as
   the first bullet:

   ```markdown
   - **The TFactory test profile now comes from the trusted contract (#1750).**
     `start_task` hands TFactory the `tfactory` block from the verified signed
     record, not from `implementation_plan.json`, which the agent can edit. When
     the record cannot be verified (store unreachable, tampered contract, or a
     host that does not isolate the agent) the handoff is still sent with an
     empty profile, TFactory infers its own plan, and a warning is logged.
     Trusted-plan tasks on hosts other than kubejob with `DATABASE_URL` therefore
     lose their declared profile; legacy tasks are unchanged.
     `pfactory.load_tfactory_block` is removed.
   ```
   → verify by `sed -n 1,20p CHANGELOG.md`.
   Traps: Unreleased only; do not touch released sections. Commit:
   `docs(changelog): step 5 note #1750`.

6. Autonomy matrix — check, regenerate only if stale.
   `$V/python scripts/gen_autonomy_matrix.py --check`. Expected: `ok: ...`
   (no matrix row names `load_tfactory_block` or this route; it passed at the
   branch head with `tiers=10 overlay=12 val=8 paths=28 gates=3 controls=13`).
   If it reports stale, run `$V/python scripts/gen_autonomy_matrix.py`, commit
   the regenerated outputs in their own commit, and record the deviation here.
   Traps: run with the venv python (system python lacks `yaml`). Do not touch
   the Factory#943 matrix cells by hand.

7. File the follow-up issue for widening `host_isolated` (decision 5; none
   exists yet, `gh issue list --search host_isolated` is empty):

   ```bash
   gh issue create --title "Widen host_isolated now that #1680 has landed" \
     --label security --body "$(cat <<'EOF'
   `host_isolated()` (`apps/web-server/server/services/trusted_contract.py:25`)
   counts only kubejob with the durable store as isolated. Its comment says
   "Revisit when #1680 lands"; #1680 has landed.

   Effect today (#1750): trusted-plan tasks on a sandbox-pidns or subprocess
   host hand TFactory an empty test profile, and the merge gate and path floor
   hold them.

   Scope: decide whether a PID-namespaced sandbox without the server env now
   counts as isolated, and update `host_isolated`, the merge gate and path-floor
   tests (`apps/web-server/tests/test_trusted_contract_merge.py`
   `test_pid_namespaced_sandbox_host_is_not_isolated`) together.

   Follow-up from #1750.
   EOF
   )"
   ```
   → verify by the printed issue URL. If the `security` label does not exist,
   rerun without `--label`. Put the URL in the PR description. No code change.
   Traps: none.

8. Final checks, then open the PR.
   Stage everything changed (`git add -A` is fine only after `git status` shows
   no stray files) and run `$V/python scripts/cq_ratchet.py --staged`; then the
   Tests section below. Open the PR against `dev` linking
   `intent/2026-10-11-1750-tfactory-block-trusted.md`,
   `spec/2026-10-11-1750-tfactory-block-trusted.md`, this plan, and the
   step 7 issue; say which steps the coder did.
   Traps: do not edit `standards/coding-standards.md`.

## Tests

| Command | Expected |
| --- | --- |
| `grep -rn load_tfactory_block apps tests` | no output |
| `$V/python -m pytest -c tests/pytest.ini tests/test_pfactory_routing.py tests/test_pfactory_tfactory_client.py apps/web-server/tests/test_trusted_contract_merge.py -q` | all pass (routing +2, tfactory_client −3, merge +2) |
| The BASE checkout from step 3, also running `-k "test_30 or test_31"` on the merge file | the new tests fail on the pre-fix code (they receive `{"lanes": ["forged"]}`) |
| `$V/python -m pytest -c tests/pytest.ini tests -q -x` | green, as on the branch head |
| `$V/pytest apps/web-server/tests -q -o asyncio_mode=auto` | green (CI's command) |
| `$V/python scripts/cq_ratchet.py --staged` | passes (no new ruff/mypy findings) |
| `$V/python scripts/gen_autonomy_matrix.py --check` | `ok: ...` |
| After deploy, on a kubejob host: edit `implementation_plan.json`'s `tfactory` on a trusted spec and start it | payload carries the signed block, or `{}` plus the "withheld" warning; never the edited block |

## Rollback

Revert the step commits (`git revert <steps 1-2> <steps 3-4> <step 5>`) or
revert the merged PR. That restores `load_tfactory_block` and the old
unchecked read; no data, schema or config changes, so nothing else to undo.
Leave the step 7 issue open; it stands on its own.

## Approver decisions (2026-10-13)

None beyond plan approval.

## Deviations during implementation

- Review follow-ups: `load_task_contract` catches `(OSError, ValueError)` so a
  non-UTF-8 contract file in the legacy state cannot 500 the start request;
  the hold test asserts exactly one "withheld" warning (decision 3).
