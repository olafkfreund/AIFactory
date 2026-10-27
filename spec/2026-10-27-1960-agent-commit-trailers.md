---
status: draft
issue: 1960
intent: intent/2026-10-27-1960-agent-commit-trailers.md
---

# Spec: agent identity, originator and tier as git trailers on factory commits

## Design

One pure helper, two call sites, one flag.

**Helper** `apps/backend/core/commit_trailers.py`, `commit_trailer_args(spec_dir: Path | None) -> list[str]`:

- Returns `[]` unless `AIFACTORY_AGENT_COMMIT_TRAILERS` is truthy, using the same idiom as `core/auth.py` (`"1"`, `"true"`, `"yes"`, `"on"`, case-insensitive). Off is the default.
- Reads `spec_dir / "requirements.json"`, the file #1897 writes and `copy_spec_to_worktree` copies whole into each worktree. A missing `spec_dir`, a missing or unparsable file, or a non-object returns `[]`; it never raises.
- Emits, in this order, `["--trailer", "Agent-Identity: <v>"]`, `["--trailer", "Autonomy-Tier: <v>"]`, `["--trailer", "Originator: <v>"]` for `agent_identity`, `intake.autonomy_tier` and `originator`.
- A value is written only if it is a non-empty string with no `\n` or `\r`. Anything else writes no trailer: unknown is absent, never a placeholder, and a line break could forge extra trailers.
- Values are written as recorded. The tier is the intake tier (`low`/`medium`/`hard`), not an `A1`-`A3` disposition: the disposition is decided per change at merge time, after these commits exist. The parent intent's `Autonomy-Tier: A3` example was written before that distinction was settled; the trailer name is kept.

`git commit --trailer` (git 2.32+) does the formatting, so the message is not hand-built. The runtime image's git is current Chainguard git.

**Call sites:**

- `core/worktree.py` `WorktreeManager.commit_in_worktree(spec_name, message, spec_dir: Path | None = None)`: appends `commit_trailer_args(spec_dir)` to the `commit` argv. The one production caller, `agents/parallel_integration.py` (the parallel-wave commit), passes its canonical `spec_dir`.
- `agents/utils.py` `commit_uncommitted_changes(project_dir, subtask_id=None, spec_dir: Path | None = None)`: same, on the safety-net commit. The caller `agents/session.py` passes its `spec_dir`.

Both new parameters default to `None`, so every existing caller and test keeps today's behaviour. The commit author is unchanged (`AIFactory <aifactory@container>`), so the originator sits beside the agent identity, never in its place.

## Alternatives rejected

- **Trailers on the coding agent's own commits** via a `commit-msg` hook or `core.hooksPath`: the only mechanism git offers for plain `git commit -m`, but it overrides or has to chain the target repo's own hooks. A separate slice (intent, remaining slice 2).
- **Trailers on the squash-merge body**: there are at least five merge paths (`pr_endgame.merge_pr`, `services/approval.py`, three providers), and the default-branch author question belongs with the GitHub App slice. Deferred.
- **Instruct the agent in the prompt to add trailers**: convention, not mechanism. An agent can forget or alter them.
- **Setting `GIT_AUTHOR_NAME` to the agent identity**: changes authorship, which the parent intent decided to leave to the GitHub App slice, and would fork the identity on every tier change.
- **Mapping the tier to `A1`-`A3` with `merge_policy`**: that is a merge disposition, not a property of the run. Writing it at commit time would record a decision not yet made.
- **Reading the values from the database or the web-server**: the backend has no such client on the commit path; `requirements.json` is already on disk inside the worktree.

## Risks

- **A commit fails because of the trailers.** `--trailer` needs git 2.32+; an older git would fail the commit only with the flag on. The flag is off by default; the image's git is far newer.
- **The safety-net commit is lost.** The helper never raises, and `commit_uncommitted_changes` already catches `CalledProcessError`/`OSError`. Flag off adds no argv at all.
- **Trailer injection.** Values come from `requirements.json`, written at intake from an authenticated principal or a provider-fetched issue author. Line breaks are refused; `--trailer` is passed as a separate argv item, so there is no shell.
- **Agent edits `requirements.json`** in its worktree before the safety-net commit and so chooses its own trailers. Accepted for this slice: like the author line, trailers are attribution, not proof (parent intent, Constraints). Proof comes with the App identity.
- Hosts: AIFactory backend pods only, and only when an operator sets the flag.

## Verification

- Unit tests for `commit_trailer_args`: flag off returns `[]` even with a full file; flag on with all three values returns the three trailers in order; missing values, a non-string, an empty string and a value with a line break are each omitted; a missing file, invalid JSON and `spec_dir=None` return `[]`.
- Integration tests with a real temporary git repo: `commit_uncommitted_changes(..., spec_dir=...)` with the flag on produces a commit whose `git log -1 --format=%(trailers)` lists the three trailers; with the flag off, no trailers. Same for `commit_in_worktree` through the existing worktree test fixtures.
- Existing `tests/test_safety_net_commit.py`, `tests/test_worktree_commit_logging.py` and `tests/test_worktree_fresh_base.py` pass unchanged.
- `ruff` and `scripts/ratchet_lint.py` (mypy ratchet) clean for touched files.
