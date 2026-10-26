---
status: approved
issue: olafkfreund/Factory#1959
spec: spec/2026-10-26-1959-pin-job-provider-creds.md
---

# Plan: Pin the provider credentials a dispatched AIFactory Job receives

Test-only slice. Decisions carried from the spec: pin the model-provider
credential names in the real Job env (suffix `_API_KEY` / `_AUTH_TOKEN` /
`_OAUTH_TOKEN`) to exactly ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY, OPENAI_COMPATIBLE_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY, OLLAMA_API_KEY;
`ANTHROPIC_API_KEY` must stay out; do not change `apps/web-server/server/services/build_backend.py`.

## Steps

1. `tests/test_build_backend_kubejob.py` (append): add the pin test → verify by running it with pytest
   (passes), then temporarily adding `MISTRAL_API_KEY` to `_PASSTHROUGH_BUILD_ENV` (apps/web-server/server/services/build_backend.py:263-340)
   and confirming it fails; revert that edit.
   Traps: no source change; clear the env with monkeypatch so the host env does not leak in.

## Tests

`pytest tests/test_build_backend_kubejob.py -q` passes; ruff clean on the test file.

## Rollback

Revert the commit. No runtime effect.
