---
title: Output-side DLP (Design)
sidebar_position: 5
---

# Output-side DLP (Design)

Status: design. This document proposes scanning agent-authored git output for
secrets and PII before it leaves the AIFactory boundary. It is not yet wired
into a runtime hook; the reuse seam and a minimal implementation are described so
the hook can be added when a chokepoint is available.

## The gap (#323, #310)

AIFactory already scans on the **input/commit** side: `validate_git_commit` in
`apps/backend/security/git_validators.py` runs the secret scanner over staged
files before every `git commit` and blocks on a hit, and auto-unstages
`.aifactory/` spec artifacts. See `apps/backend/security/scan_secrets.py`.

But a coding agent produces more outbound text than commit *contents*:

- **Commit messages** — scanned only incidentally; a secret pasted into a commit
  message body is not caught by the staged-file scan.
- **PR title and body** — authored by the agent at PR-endgame and pushed to
  GitHub. Never scanned.
- **PR/issue comments** — agent replies during review handback. Never scanned.

Any of these can exfiltrate a credential or personal data past the boundary even
when the committed *files* are clean.

## Reuse seam

The existing scanner already scans arbitrary strings — no new pattern engine is
needed:

- `scan_secrets.scan_content(content: str, file_path: str) -> list[SecretMatch]`
  runs every pattern in `ALL_PATTERNS` (generic API-key/token/password/base64
  plus service-specific: OpenAI/Anthropic, AWS, GCP, GitHub PAT, ...) over any
  text, applying the same `is_false_positive` filter used at commit time.
- `git_validators._format_secret_error(matches)` already renders matches into an
  actionable, agent-readable message.

So DLP on outbound text is: pass the text to `scan_content`, and on a non-empty
result either block (raise / refuse to create the PR) or redact.

## Proposed design

1. **PII patterns.** Extend the pattern set with a small, high-precision PII
   group (email, phone, national-id/SSN-shaped, credit-card via a Luhn check) in
   `scan_secrets.py`, guarded so the false-positive filter keeps example/test
   data quiet. Reused automatically by every caller of `scan_content`.
2. **One choke helper.** Add a thin
   `dlp.scan_outbound(text, label) -> list[SecretMatch]` that wraps
   `scan_content(text, label)` and applies a `.dlpignore` allowlist (mirroring
   `.secretsignore`). One place to reason about outbound policy.
3. **Wire at the boundaries**, all fail-closed on a hit:
   - PR create — scan `title + body` before the GitHub API call; refuse and
     surface the matches to the agent (same UX as a blocked commit) so it
     rewrites the text.
   - PR/issue comment — scan the comment body before posting.
   - Commit message — scan the message alongside the existing staged-file scan in
     `validate_git_commit`.
4. **Mode flag**, mirroring the model registry and the commit scanner:
   `AIFACTORY_OUTPUT_DLP=block` (default at the boundary) / `warn` / `off`, so
   operators can dial enforcement without code changes.
5. **Audit.** On a block, record `{label, pattern_name, masked_match}` (never the
   raw secret — use `scan_secrets.mask_secret`) to the audit log so the event is
   evidence, not just a refusal.

## Why design-only for now

The commit-message case has an obvious home (`validate_git_commit`) and could
ship as a small safe addition. The PR title/body/comment cases need the
PR-endgame code path to route through a single scan chokepoint first; adding a
scanner with no caller would be dead code. The reuse seam above means each hook
is a few lines once its chokepoint exists — the scanning primitive
(`scan_content`) and the error rendering (`_format_secret_error`) are already in
place.
