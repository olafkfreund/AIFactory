# Reddit & social variants

Each community rewards a different angle. Same product, different lead. Keep self-promo honest and
follow each sub's self-promotion rules (engage, don't drive-by). Same truth gate as the rest of the
kit applies.

---

## r/selfhosted

**Title:** I built an open-source, self-hostable AI coding agent — your code never leaves your box

**Body:**

Most AI coding tools are cloud SaaS — you send them your repo. I wanted one I could run entirely on
my own hardware, so I built AIFactory and open-sourced it (MIT).

It turns a task into code through a pipeline you can watch: spec → plan → code → QA, with you
approving the plan and the diff. Runs via docker-compose or a Helm chart on your own Kubernetes.
Bring your own model — Claude/OpenAI/Gemini or a **local Ollama model**, so nothing has to leave
your network. Every task runs in an isolated git worktree and every action is in a hash-chained
audit log.

Solo dev, full-time on it. Would love feedback from this crowd on the self-hosting story
specifically — what's annoying about deploying it, what's missing. Repo + demo: [link]

---

## r/devops

**Title:** Open-source autonomous coding platform with a real audit trail + Helm deploy (self-hosted)

**Body:**

Sharing a project I've been building solo: AIFactory — a spec-first, review-gated autonomous coding
pipeline (planner → coder → QA) that you self-host. Relevant to this sub because the focus is the
ops/governance side, not the demo magic:

- Helm chart, OpenTelemetry tracing across HTTP/DB/agent/Redis, multi-replica with S3 workspace
  snapshots + Redis fan-out.
- Hash-chained audit log; specs/plans/QA reports on disk and in version control.
- Per-task git-worktree isolation; gVisor sandbox opt-in.
- Multi-provider (Claude/OpenAI/Gemini/Ollama/OpenAI-compatible) — no single-vendor lock-in.
- Enterprise bits (multi-tenant, SAML/SCIM) are beta; core is MIT and free.

What would you want to see before running something like this in a real cluster? Repo + demo: [link]

---

## r/LocalLLaMA

**Title:** Self-hosted autonomous coding agent that runs against your local Ollama model (open-source)

**Body:**

If you want an autonomous coding agent (spec → plan → code → QA, not just completion) that runs
against a **local model** instead of a cloud API, AIFactory might be your thing. Multi-provider by
design — point it at Ollama or any OpenAI-compatible endpoint (LM Studio, vLLM, etc.) and your code
+ prompts stay on your hardware. You can even route per phase (e.g. a bigger model to plan, a local
model to code).

It's MIT, self-hosted (docker-compose or Helm), spec-first and review-gated, with an audit log. I'm
a solo dev and I'd love to know which local models people get the best coding results from in this
kind of pipeline. Repo + demo: [link]

> Note: confirm the local-only setup is genuinely turnkey before leading with this — this sub will
> test it immediately and call out any cloud dependency.

---

## LinkedIn (founder voice)

After months building it, I've open-sourced **AIFactory** — an AI software engineer you can
self-host and audit.

The premise: AI can write code, but most tools make you choose between sending your source to
someone's cloud and trusting an unsupervised diff. For a bank, a hospital, or a government team,
both are non-starters. AIFactory is built for that gap — spec-first, review-gated, fully auditable,
runs in your own Kubernetes, works with any model including local ones.

It's MIT-licensed and free. If your organization wants AI productivity but can't use the cloud
tools, I'd love to talk about what would make this work for you. [link]

---

## X / Mastodon (thread starter)

1/ I open-sourced AIFactory: the AI software engineer you can self-host and audit.

Most AI coding tools = send your repo to the cloud + trust an unreviewed diff. Can't do that at a
bank. So I built the opposite. 🧵

2/ Spec → plan → code → QA. You approve the plan and the diff. Every action in a hash-chained audit
log. Runs in YOUR cluster (Helm) or laptop (docker-compose).

3/ Any model — Claude, GPT, Gemini, or local Ollama. No lock-in, you own the model bill.

4/ MIT-licensed, solo + full-time. Repo + 2-min demo 👇 [link] — feedback very welcome, especially
from folks in regulated shops.
