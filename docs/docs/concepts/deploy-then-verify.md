---
title: Deploy-then-verify
sidebar_position: 12
---

# Deploy-then-verify

Tests that never run against a deployment aren't verification — they're a
hopeful diff. **Deploy-then-verify** closes that gap: on a clean build AIFactory
ships the services to **real AWS App Runner** with deterministic Terraform,
proves the live HTTPS endpoint works, hands the deployed URL to the verification
leg, and tears everything down — cost-guarded and repeatable.

It is **opt-in and default off.** With `AIFACTORY_AUTO_DEPLOY` unset, the build
behaves exactly as before.

## What happens on a clean build

When `AIFACTORY_AUTO_DEPLOY` is enabled, after the coder/QA loop finishes:

1. **Render deterministic infrastructure.** AIFactory generates the App Runner
   Terraform — not an LLM guess. Every resource is tagged `factory-ephemeral`
   and `spec_id`, and the teardown workflow is emitted *with* the deploy
   workflow, never without.
2. **Build + push images, `terraform apply`.** ECR repos and an IAM role are
   created, the service images are built and pushed, and App Runner stands up
   live HTTPS services.
3. **Capture the live URL.** The deployed URLs are read back from the
   `deployed_urls.json` artifact; the first service URL becomes the primary
   `deployed_url` (the base target the verification leg points at).
4. **Verify against the live endpoint.** The acceptance-criteria tests run
   against the deployed URL, not a local process — so drift between "builds"
   and "actually runs" is caught.
5. **Tear down.** `terraform destroy` removes everything that came up.

The result is written to `deploy_result.json` at the worktree root:

```json
{ "repo": "...", "branch": "...", "head_sha": "...", "deployed_url": "https://...", "deployed": true }
```

That object is what travels downstream to TFactory for live-deployment
verification.

## Why the teardown is the load-bearing part

Real provisioning is only safe if it reliably un-provisions, so teardown is
built three ways:

- The destroy workflow **always ships with** the deploy workflow — there is no
  code path that produces one without the other.
- The deploy orchestrator fires teardown automatically on **any** failed or
  timed-out deploy.
- A tagged sweeper destroys anything tagged `factory-ephemeral` older than an
  hour, catching anything that ever slips through.

Because every resource carries the `factory-ephemeral` + `spec_id` tags, the
teardown can never reach beyond what the Factory created — your existing
infrastructure is never touched.

## Enabling it

Off by default. Turn it on per project via the project's `.aifactory/.env`, or
globally on the web server:

```bash
AIFACTORY_AUTO_DEPLOY=true
```

### Prerequisites

- An **AWS account** with permission to create App Runner, ECR, and the scoped
  IAM role (the GitHub Actions role is scoped to `AppRunner*` / `ECR*` / `IAM`).
- A **GitHub repo with Actions enabled** (the deploy/destroy run as workflows).
- **AWS credentials** available to the workflow as repo secrets
  (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`).

## Try it without wiring a project

Two reproducible demo commands exercise the whole loop against a real account
and tear it down at the end:

- `/run-aws-demo` — deploys two FastAPI services, proves the live API, runs the
  acceptance tests against the deployed URLs, then destroys everything.
- `/run-aws-webtest` — deploys an authenticated web form and has a browser test
  log in, exercise the UI, and record screenshots + findings as proof.

## Known limitations

- **Single App Runner instance per service** — no multi-replica coordinated
  deploy yet.
- **Teardown is a separate workflow** — best-effort; if GitHub Actions is
  unavailable the tagged sweeper is the backstop, and resources are tagged for
  easy manual identification.

## See also

- [Testing authenticated web apps](https://github.com/olafkfreund/AIFactory/blob/dev/guides/testing-authenticated-web-apps.md)
- [Task Contract](../task-contract) — how the deployed URL travels to verification
- [Configuration Reference](../configuration-reference)
