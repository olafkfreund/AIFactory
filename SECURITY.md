# Security Policy

Thanks for helping keep AIFactory and its users safe.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

Use GitHub's private reporting:

> https://github.com/olafkfreund/AIFactory/security/advisories/new

This routes the report directly to the maintainers via a private
GitHub Security Advisory.

Include, where possible:

- A description of the issue and its impact
- Steps to reproduce (PoC welcome)
- Affected version / commit SHA
- Suggested mitigation, if any

## Response targets

| Stage              | Target                  |
|--------------------|-------------------------|
| Acknowledgement    | within 3 business days  |
| Triage + severity  | within 7 business days  |
| Fix or mitigation  | depends on severity     |
| Public disclosure  | coordinated with reporter (typically after a fix ships) |

We will credit reporters in the advisory unless anonymity is requested.

## Supported versions

Only the **latest minor release** receives security fixes. Older minors
may receive backports at maintainers' discretion. See [RELEASE.md](RELEASE.md)
and [CHANGELOG.md](CHANGELOG.md) for current versions.

## Scope

In scope:

- The AIFactory backend, web-server, and frontend in this repository
- The default Docker image built from this repository's `Dockerfile`
- Default agent prompts and tool integrations shipped here

Out of scope:

- Third-party providers (Anthropic, OpenAI, Ollama, Linear, GitHub) —
  report to those vendors directly
- Vulnerabilities that require local root or physical access
- Self-inflicted issues from disabling auth, weakening CORS, or running
  with `permission_mode="bypassPermissions"` outside the documented use
- Issues in user-supplied custom skills, MCP servers, or hooks

## Agent command allowlist

The Bash allowlist, the per-command validators and `AIFACTORY_EGRESS_POLICY`
are guardrails against accidental and reflexive misuse. They are not
containment. The containing boundary is the Kubernetes Job pod each agent
session runs in (#1932). Make targets and npm/yarn/pnpm scripts are allowed
only when they are present at the commit the build started from (#1948).
These still run, and the allowlist cannot see inside them:

- a changed recipe or script body;
- npm `pre`/`post` and install lifecycle scripts;
- `bash x.sh`, interpreters and test runners.

A report that only shows one of these, inside the Job pod, is expected
behaviour.

## Safe-harbor

Good-faith research conducted under this policy will not be subject to
legal action by the maintainers. Please act in good faith: avoid
privacy violations, data destruction, or service degradation.
