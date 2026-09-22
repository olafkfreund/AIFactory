---
status: draft
issue: 1569
intent: intent/2026-09-23-1569-task-created-at-stamp.md
---

# Spec: Report the recorded creation time, and label a guess as a guess

## Design

Three sources, in order, in `spec_to_task` (`routes/task_service.py:960`):

1. **The recorded stamp.** `requirements.json`'s `created_at`, written by every creation
   path. `load_spec_metadata` (`:494-516`) **already parses that file** for the title, so
   this costs no extra I/O: it returns the value alongside the others, and `spec_to_task`
   prefers it.
2. **The spec's own files.** When there is no stamp, the oldest `st_mtime` of
   `requirements.json` and `spec.md`. Both are written when the task is created; the
   directory's `st_ctime` moves whenever *anything* inside it is written, which is the whole
   defect. This is still an estimate, but a far better one.
3. **The directory's ctime**, as today, if neither file can be read.

Sources 2 and 3 set a new field `created_at_is_estimate: bool` on `Task`
(`routes/task_models.py:125`, default `False`). `created_at` stays a required ISO string in
every case, so existing consumers — the board sort at `routes/tasks.py:186`, the frontend's
`Task` type, CFactory — keep working untouched. The UI can mark an estimate later; that is
not in this change.

**No backfill.** Spec directories are gitignored (`.gitignore:72`), so there is no commit
history to recover a real date from, and inventing one is worse than labelling the estimate.
The ~18 known specs get source 2, which is already closer to the truth than what they show now.

## Alternatives rejected

- **Return `null` for an unknown creation time.** Most honest, but `created_at` is a required
  string in the API and the board sorts on it; a null would break sorting in consumers we do
  not control. The flag carries the same information additively.
- **Recover dates from git.** Impossible: the directories are not in git.
- **Stamp the missing ones now** (write a `created_at` into old `requirements.json` files).
  That writes a fabricated value into the record, and the intent rules it out.
- **Keep the directory ctime as the only fallback.** It is the defect: any write re-dates the
  task, which is exactly how 18 tasks came to read 2026-09-18.

## Risks

- **A wrong stamp is now trusted.** If a creation path ever wrote a bad `created_at`, it is
  reported verbatim instead of being masked by ctime. Mitigation: parse strictly — a value
  that is not an ISO-8601 string falls through to source 2, with a debug log.
- **Order changes visibly.** Tasks will re-sort on the board the first time this ships, because
  they are finally in real creation order. That is the point, but it will look like a change.
- **`load_spec_metadata` is shared**, so the added key must be additive and never replace an
  existing one.
- `updated_at` keeps using `st_mtime` and is unchanged.

## Verification

- Unit (`spec_to_task`): a spec whose `requirements.json` carries `created_at` reports exactly
  that value with `created_at_is_estimate` false — **and still does after the directory is
  touched** (the #1569 case: write a file, re-read, the value does not move).
- Unit: no stamp → the oldest of the two file mtimes, flag true. Neither file readable → dir
  ctime, flag true. A malformed stamp → falls through to the estimate, flag true.
- The API still returns a valid `Task` for every existing spec (no required field is dropped).
- `routes/tasks.py` listing still sorts, with the estimate-flagged ones ordered by their
  estimate.
