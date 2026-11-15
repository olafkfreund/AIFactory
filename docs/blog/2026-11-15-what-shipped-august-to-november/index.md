---
slug: what-shipped-august-to-november
title: "What shipped in AIFactory, August to November 2026"
authors: [olaf]
tags: [ai-coding, security, compliance, kubernetes, autonomous]
date: 2026-11-15
description: Auto-merge gated on evidence, and the coding agent treated as untrusted. Releases v3.6.74 to v3.10.16, with the opt-in flags and behaviour changes stated.
---

AIFactory has shipped releases from
[v3.6.74](https://github.com/olafkfreund/AIFactory/releases/tag/v3.6.74) (2026-08-18) to [v3.10.16](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.16) (2026-10-31); the
[release list](https://github.com/olafkfreund/AIFactory/releases) has the notes.
Nothing has been tagged since. Several 3.10.x releases are marked as behaviour
changes for operators, and this post says so where it matters. Work that is
only on `dev` is at the end, under "In progress".

## Auto-merge gated on evidence

- A verified task auto-merges only on a TFactory verdict plus green host CI
  that AIFactory fetches itself for the exact head commit. A host without
  `TFACTORY_BASE_URL`, or a repo without CI, never auto-merges a verified task
  and times out to a human after 20 minutes
  ([v3.10.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.0), [#1695](https://github.com/olafkfreund/AIFactory/issues/1695)).
- Tasks with no signed contract go through the same gate. This is breaking for
  projects without TFactory or without CI, whose tasks no longer auto-merge
  ([v3.10.3](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.3), [#1697](https://github.com/olafkfreund/AIFactory/issues/1697)).
- The merge gate acts only on the contract PFactory signed, stored server-side.
  A trusted task auto-merges only if it was built as a Job (`kubejob`, with
  `DATABASE_URL`), and the server-side `trusted_contracts` table needs an Alembic
  migration. The notes say auto-merge is held for all other hosts
  ([v3.9.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.9.0), [#1667](https://github.com/olafkfreund/AIFactory/issues/1667)).
- A task whose contract declares a production target or high risk class is held
  for a human, and so is a task with an unreadable contract
  ([v3.8.1](https://github.com/olafkfreund/AIFactory/releases/tag/v3.8.1), [#1658](https://github.com/olafkfreund/AIFactory/issues/1658)).
- A `human-approval` gate clears only on a GitHub review that approves the head
  commit, from someone other than the PR author and not a bot. Until AIFactory
  has its own bot identity, such tasks merge by hand
  ([v3.9.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.9.0), [#1663](https://github.com/olafkfreund/AIFactory/issues/1663)). The optional GitHub App identity
  (`AIFACTORY_GITHUB_APP_*`) is off unless configured
  ([v3.10.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.0), [#1671](https://github.com/olafkfreund/AIFactory/issues/1671)).

## The coding agent is treated as untrusted

- The web server sets itself non-dumpable so in-pod agents cannot read
  `/proc/1/environ`
  ([v3.8.3](https://github.com/olafkfreund/AIFactory/releases/tag/v3.8.3), [#1679](https://github.com/olafkfreund/AIFactory/issues/1679)).
- Non-Claude agent CLIs (Codex, Gemini/Antigravity, Copilot, OpenCode) and the
  Ollama/OpenAI-compatible tool loops start with scrubbed environments plus only
  their own key
  ([v3.10.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.0), [#1692](https://github.com/olafkfreund/AIFactory/issues/1692)). Each spawn gets a private 0700
  HOME and an allowlist, which is a behaviour change: operators add names
  through `AIFACTORY_AGENT_ENV_EXTRA` ([v3.10.3](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.3), [#1762](https://github.com/olafkfreund/AIFactory/issues/1762)).
  The notes list an open gap: on local runners the same-uid `/proc` window stays
  open ([#1724](https://github.com/olafkfreund/AIFactory/issues/1724)).
- Server-side git is pinned against agent-written config (hooks, fsmonitor,
  credential helper, filters) ([v3.10.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.0), [#1689](https://github.com/olafkfreund/AIFactory/issues/1689)).
- The Bash allowlist parser checks commands hidden in wrappers, redirects,
  heredocs and runner prefixes, and fails closed on non-literal command words
  ([v3.10.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.0), [#1720](https://github.com/olafkfreund/AIFactory/issues/1720)).
- Each run gate is read once into a frozen snapshot before the first agent
  session. The notes say cross-run persistence is not closed
  ([v3.10.16](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.16), [#1986](https://github.com/olafkfreund/AIFactory/issues/1986)).

## Identity, access control and audit

- `acw_` API keys authenticate for the first time (before, every real key
  failed), with checks for expiry, owner status, role cap and org binding
  ([v3.10.12](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.12), [#1914](https://github.com/olafkfreund/AIFactory/issues/1914)).
- Remote MCP authorizes each tool call as the calling key, and stays off by
  default (`AIFACTORY_MCP_REMOTE_ENABLED`)
  ([v3.10.12](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.12), [#1917](https://github.com/olafkfreund/AIFactory/issues/1917)).
- OIDC ID-token `iss`, `aud` and `exp` are enforced and HS256 and `alg=none` are
  refused. The ID token `iss` must match `APP_OIDC_ISSUER_URL`, which the notes
  call an operator-facing change
  ([v3.10.14](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.14), [#1957](https://github.com/olafkfreund/AIFactory/issues/1957)).
- Authorization denials and gate rejections are committed to the audit chain
  instead of being rolled back with the request
  ([v3.10.13](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.13), [#1936](https://github.com/olafkfreund/AIFactory/issues/1936)).

## Data protection

- Outbound PII scrubbing is on by default and fails closed. Only the built-in
  high-precision set (hyphenated SSN, email, US phone, Luhn-valid cards) is
  scrubbed outbound ([v3.6.75](https://github.com/olafkfreund/AIFactory/releases/tag/v3.6.75), [#320](https://github.com/olafkfreund/AIFactory/issues/320)).
- A project `dataClassification` of `regulated` always scrubs outbound, and the
  kill-switch is ignored ([v3.10.8](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.8), [#1862](https://github.com/olafkfreund/AIFactory/issues/1862)).
  Direct `claude --print` spawns were covered in the next release
  ([v3.10.9](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.9), [#1870](https://github.com/olafkfreund/AIFactory/issues/1870)).

## Build quality: QA, gates and evidence

- QA no longer signs off a build that produced nothing: approval needs a commit
  not yet on origin or an uncommitted change
  ([v3.6.75](https://github.com/olafkfreund/AIFactory/releases/tag/v3.6.75), [#1396](https://github.com/olafkfreund/AIFactory/issues/1396)).
- QA refuses sign-off when the spec enumerates an API the build did not export,
  and the enumerated API reaches the coder as a hard requirement
  ([v3.6.79](https://github.com/olafkfreund/AIFactory/releases/tag/v3.6.79), [#1429](https://github.com/olafkfreund/AIFactory/issues/1429)).
- Test evidence is scoped per subtask and must touch the deliverable
  ([v3.6.75](https://github.com/olafkfreund/AIFactory/releases/tag/v3.6.75), [#1187](https://github.com/olafkfreund/AIFactory/issues/1187)).
- An opt-in repo lint gate (`AIFACTORY_REPO_LINT_GATE=1`) runs the repo's CI
  linters that have a curated command. It is off by default because it can fail
  builds that pass today ([v3.10.3](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.3), [#1742](https://github.com/olafkfreund/AIFactory/issues/1742)).
- Apply-correction requires a `triage` block by default
  (`APP_REQUIRE_CORRECTION_TRIAGE`)
  ([v3.10.9](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.9), [#1872](https://github.com/olafkfreund/AIFactory/issues/1872)).

## Models and operations

- Opus 5.5 is the flagship for spec, planning and QA, and Sonnet 5 codes. The
  notes say the Opus 5.5 pricing is provisional
  ([v3.8.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.8.0)).
- Build Jobs declare an ephemeral-storage limit, and a crashed dispatch no
  longer leaks a concurrency slot
  ([v3.7.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.7.0), [#1606](https://github.com/olafkfreund/AIFactory/issues/1606)). Postgres pools are capped at 2+2
  connections per pod ([v3.10.15](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.15), [#1971](https://github.com/olafkfreund/AIFactory/issues/1971)).
- The model gateway image is published, but the notes state nothing deploys it
  ([v3.10.12](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.12), [#1912](https://github.com/olafkfreund/AIFactory/issues/1912)).
- Completion events use the correlation key `<repo>#<n>`, and
  `af-<project_id>:<spec_id>` where no repo is known. This changed twice, and
  the notes list behaviour changes for tasks in flight
  ([v3.10.15](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.15), [#1966](https://github.com/olafkfreund/AIFactory/issues/1966)).

## In progress

Merged to `dev` and in no release; 33 commits ahead of `main` at the time of
writing:

- Spec creation as a Kubernetes Job, behind `AIFACTORY_SPEC_BACKEND`
  ([#2026](https://github.com/olafkfreund/AIFactory/issues/2026)).
- Per-org GitHub App installation tokens ([#2039](https://github.com/olafkfreund/AIFactory/issues/2039)).
- `/ws/events` confined to the caller's orgs, and scoped `acw_` keys on
  WebSocket ([#2027](https://github.com/olafkfreund/AIFactory/issues/2027), [#2017](https://github.com/olafkfreund/AIFactory/issues/2017)).
- Keeping the control plane's GitHub token out of agent children
  ([#2030](https://github.com/olafkfreund/AIFactory/issues/2030)).
