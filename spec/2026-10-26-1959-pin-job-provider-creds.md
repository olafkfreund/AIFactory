---
status: approved
issue: olafkfreund/Factory#1959
intent: intent/2026-10-26-1959-pin-job-provider-creds.md
---

# Spec: Pin the provider credentials a dispatched AIFactory Job receives

## Design

Add one test to `tests/test_build_backend_kubejob.py`. It sets every name in `_PASSTHROUGH_BUILD_ENV`
(plus `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY`) to a dummy value, builds
the Job env through the real builder, keeps the names that look like a model-provider
credential (suffix `_API_KEY`, `_AUTH_TOKEN` or `_OAUTH_TOKEN`), and asserts that
set equals a pinned frozenset:

ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY, OPENAI_COMPATIBLE_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY, OLLAMA_API_KEY.

`ANTHROPIC_API_KEY` is set but must not appear (OAuth-only policy). `GITHUB_TOKEN`,
`GH_TOKEN` and `S3_*` are not model-provider credentials and fall outside the
suffix filter; they are not the gateway's concern.

## Alternatives rejected

- Assert on the constant directly: misses a credential injected outside the
  tuple (the OAuth token is added separately).
- Remove credentials from the Job now: breaks running builds; forbidden in this slice.
- One shared test in Factory: the allowlists live in two repos and each must
  fail its own CI.

## Risks

None at runtime: test only. The pin will fail when someone legitimately adds a
credential; that is the point, and the failure message says to update the pin.

## Verification

The new test passes on the current code; adding a name such as
`MISTRAL_API_KEY` to `_PASSTHROUGH_BUILD_ENV` makes it fail (checked locally, not committed).
