---
status: draft
issue: 2014
intent: intent/2026-11-07-2014-enforce-admins-claim.md
---

# Spec: make the control mapping's branch-protection claim match what holds now

## Design

One data edit, then regenerate. No code changes.

1. In `docs/compliance/control-objectives.toml`, replace the `claim` value on
   the six controls that carry it (`policy.tier.low`, `policy.tier.medium`,
   `policy.tier.hard`, `gate.merge.merge_policy`,
   `gate.server.services.pr_review_service`, `gate.server.services.pr_endgame`;
   lines 30, 36, 42, 84, 90, 96 today) with this exact text:

   > Enforced by branch protection. enforce_admins is on for main in AIFactory,
   > PFactory, TFactory, CFactory and Factory (applied 2026-10-22, re-read
   > 2026-11-07, Factory#943), so there the required checks bind admins too. It
   > is off on every dev branch and on factory-gitops main, where the checks bind
   > non-admin merges only: the fleet's merge paths do not use admin bypass, but
   > GitHub does not prevent it.

   It states the setting and its scope, not a stronger guarantee. It contains
   no `|`, because the generator puts `claim` in a markdown table cell
   (`scripts/gen_autonomy_matrix.py:874`).

2. Update the comment above the tables (`control-objectives.toml:22-24`). It
   now says the claim is "verbatim from the step 0 measurement". It will say
   the claim comes from the step 0 measurement (2026-10-07) as updated by
   #943's decision (applied 2026-10-22, re-read 2026-11-07, AIFactory#2014).

3. Run `python scripts/gen_autonomy_matrix.py` to rewrite
   `docs/docs/compliance/autonomy-matrix.md` and
   `docs/static/compliance/autonomy-matrix.json`. These are never hand-edited.

The generator, its `--check`, and the workflow stay as they are.

## Alternatives rejected

- **Derive the claim from the live GitHub API at generation time.** That makes
  `--check` depend on the network and a token, and on live state that can
  change between runs. A dated, measured claim is the existing convention.
- **Drop the claim now that main is protected.** `dev` and factory-gitops
  `main` still allow an admin bypass, and the page must say what holds.
- **Add a test pinning the claim text** (agy's intent finding 4). `--check`
  already fails when the outputs do not match the TOML, and a literal-string
  test would only duplicate the TOML.

## Risks

- The claim goes stale again if `enforce_admins` changes, which could happen
  through the break-glass in Factory `docs/compliance/branch-protection.md`.
  The dates make that visible. Keeping it current is out of scope here.
- The generator may need backend imports to run. The plan runs it the way CI
  does, with `apps/backend/.venv` or an equivalent venv.
- The hub row (`Factory/docs/compliance/control-matrix.md:53`) is fixed in a
  separate one-line PR. If Factory#324's open branch edits the same line,
  whichever merges second gets a merge conflict there.

## Verification

- `git grep -n "while enforce_admins is false"` in this repo, outside
  intent/spec/plan, returns nothing.
- `python scripts/gen_autonomy_matrix.py --check` exits 0.
- The diff touches only the TOML and the two generated outputs, apart from the
  artifacts. The JSON diff changes only `claim` strings.
- CI's `autonomy matrix matches the policy` is green on the PR.
