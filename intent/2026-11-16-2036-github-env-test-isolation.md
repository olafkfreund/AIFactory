---
status: draft
issue: 2036
author: olafkfreund
---

# Intent: isolate tests/test_github_env.py from the host's gh login

## Problem

`test_helper_reset_keeps_store_from_saving_token` and
`test_real_gh_reads_token_without_rewriting_hosts` run the real `gh` and
`git credential` on the test host. On a host logged in to GitHub the returned
token is the host's, the assertion fails, and the assertion message prints the
real token into terminals, agent transcripts and CI logs.

## Proposed outcome

Both tests pass on a host with a live `gh` login, never read host credentials,
and a failing assertion never prints a token-shaped value.

## Affected users and systems

Developers and agents running `pytest tests/test_github_env.py`. PFactory and
TFactory have no `test_github_env.py` or equivalent real-`gh` test (checked on
origin/dev), so they are out of scope.

## Constraints

No product code change. Never run the leaking tests on a logged-in host before
the fix.

## Open questions

None.
