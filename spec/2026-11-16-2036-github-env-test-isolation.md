---
status: draft
issue: 2036
intent: intent/2026-11-16-2036-github-env-test-isolation.md
---

# Spec: isolate test_github_env.py

## Design

A test helper `_host_isolation(tmp_path, env)` builds a stub `bin/gh` and puts it
first on PATH; sets HOME, XDG_CONFIG_HOME to tmp, GIT_CONFIG_GLOBAL=/dev/null (or
the test's own gitconfig) and GIT_CONFIG_NOSYSTEM=1. GH_CONFIG_DIR stays the
managed dir that `github_env` creates (the thing under test). The stub `gh`
implements `auth token` and `auth git-credential get|store|erase` by reading
`oauth_token` from `$GH_CONFIG_DIR/hosts.yml` (JSON), never touching the host.
Assertions compare via a `_redact` helper and a fixed message, so a failure
shows only `<token:N chars>`.

## Alternatives rejected

- Keep real gh with a tmp HOME: gh may still reach the host keyring over dbus.
- Skip when logged in: hides the test where it matters.

## Risks

The stub no longer proves real gh parses hosts.yml. Accepted: the issue asks for
stubs, and hosts.yml content/mode is still asserted in another test.

## Verification

Run the file with a fake PATH-less gh login simulated by a poisoned
`GH_TOKEN`-free host; pytest passes; force a failure and check the output has no
token.
