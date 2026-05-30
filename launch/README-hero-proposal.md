# Proposed README repositioning

This is a **proposed** rewrite of the top of `README.md` — the hero, "What it is", and "Why it's
different" — re-anchored on the self-host / auditable / governed positioning. It is **not** applied
to `README.md` automatically; review and diff it in deliberately. Everything below the "Why it's
different" section in the current README (Quickstart, See it work, Screenshots, Docs, Stack,
Contributing, License) stays as-is.

> Truth check before applying: the Quickstart still requires `claude setup-token`. Keep that
> accurate — either ship the zero-config local path first, or keep the Quickstart honest and let the
> hero say "bring your own model (Claude seat or a local Ollama model)" without a "no API key" claim.

---

```markdown
<div align="center">
  <img src="apps/frontend-web/public/logo.png" alt="AIFactory" width="120" />

# AIFactory

**The open-source AI software engineer you can self-host and audit.**
Spec-first · review-gated · multi-model · runs in your own cluster — your code never leaves it.

[![CI](https://github.com/olafkfreund/AIFactory/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/olafkfreund/AIFactory/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-olafkfreund.github.io%2FAIFactory-blue)](https://olafkfreund.github.io/AIFactory/)
[![License](https://img.shields.io/badge/license-MIT%20OR%20GPL--3.0-blue.svg)](LICENSE)

[Docs](https://olafkfreund.github.io/AIFactory/) ·
[Why AIFactory](https://olafkfreund.github.io/AIFactory/why-aifactory) ·
[Demo](https://olafkfreund.github.io/AIFactory/demo) ·
[Architecture](https://olafkfreund.github.io/AIFactory/architecture/overview) ·
[Roadmap](https://olafkfreund.github.io/AIFactory/roadmap)

</div>

---

## What it is

Most AI coding tools ask you to either ship your source to someone else's cloud, or trust an
unsupervised agent's diff. If you work somewhere that can't do either — a bank, a hospital, a
government team — you've been stuck.

AIFactory turns a task into shipping code through a pipeline you can **watch and verify**:
**spec → plan → code → QA**, each step written down, each task isolated in its own git worktree,
every action recorded in a hash-chained audit log. Bring your own model (Claude, OpenAI, Gemini,
Codex, or a local Ollama / OpenAI-compatible endpoint — no lock-in). Self-host it on your own
Kubernetes with the Helm chart, or run it on a laptop with docker-compose.

You watch the whole thing happen live in the **Agent Console** — read-only by default, one-click
Attach when you want to drive.

## Why it's different

- 🔒 **Self-hosted, in your perimeter.** Your source never has to leave your network.
- 📝 **Spec-first, not vibe-first.** Every run starts from a written, reviewable spec with
  acceptance criteria. You approve the plan before code is written.
- 🔁 **Review-gated.** You approve the diff before it merges; a QA agent checks it against the spec.
- 🧾 **Auditable.** Hash-chained audit log; specs, plans, and QA reports on disk and in version
  control. SOC2 / ISO evidence in the enterprise build.
- 🔌 **No vendor lock-in.** Any model, per phase — including fully local.
- 🧩 **Isolated by default.** Each task runs in its own git worktree; nothing touches your tree
  until you merge.

> **Status:** the spec → plan → code → QA pipeline, web UI, multi-provider routing, worktree
> isolation, and audit log are production-grade. Enterprise modules (multi-tenant isolation,
> SAML/SCIM) are in beta — see the [roadmap](https://olafkfreund.github.io/AIFactory/roadmap).
```

---

## Why not auto-applied

The current `README.md` was rewritten recently and contains accurate, current content (shipset
notes, demo script, screenshots). Overwriting it wholesale would destroy that. Applying this is a
one-section diff you should make consciously — and ideally on the same PR that lands the demo video
link and the capability matrix, so the README's promises and the product's reality ship together.
