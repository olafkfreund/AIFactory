# AIFactory — Product & Go-To-Market Strategy

> Status: Adopted · 2026-05-30
> Spine: OSS-adoption-led · open-core · wedge = regulated self-hosters · North Star = community traction

## Context

AIFactory is a spec-first autonomous software-engineering platform (planner → coder → QA with
human-review gates), multi-provider by design, with a polished web UI and a deliberate,
already-shipped enterprise/compliance backbone (Kubernetes Helm, per-tenant isolation, SAML/SCIM,
hash-chained signed audit logs, OpenTelemetry, gVisor). This memo records the positioning and
go-to-market decisions and the reasoning behind them. The launch collateral derived from it lives
under [`/launch`](https://github.com/olafkfreund/AIFactory/tree/dev/launch).

## The four anchoring decisions

1. **Primary goal:** open-source adoption — *North Star: community traction* (stars, contributors,
   "on the map") over the next 6–12 months.
2. **Wedge:** platform & security engineers who *must* self-host — regulated verticals (banking,
   healthcare, government, defense).
3. **Motion:** open-core, bottom-up. The enterprise/compliance moat is the *differentiator that
   drives OSS adoption*, not a top-down sales motion (infeasible solo).
4. **Monetization (door kept open):** open-core — core free/OSS, enterprise edition paid later.

**Central insight:** the narrow regulated wedge is not a constraint on reach — it is the *hook*
that earns broad attention. "Governed, auditable, self-hostable autonomous coding" is a
contrarian, opinionated stance in a market saturated with hype. Sharpness travels; me-too "it
writes code" does not.

## 1. What AIFactory is today (verified)

**Shipped & working:** spec-first pipeline with human gates and per-task git-worktree isolation; a
genuinely polished web product (Kanban, Monaco editor, integrated terminals, live agent console,
GitHub PR review, onboarding, theming, i18n); a real multi-provider engine (Claude primary; Codex,
Gemini, Ollama, any OpenAI-compatible, Bedrock/Vertex via LiteLLM); and a substantial
enterprise/compliance backbone (tenant isolation, audit anchors, OTel, gVisor, S3+Redis
multi-replica), with SAML/SCIM and the LiteLLM gateway in flight.

**Honest gaps:** Graphiti memory is optional/underused; SAML/SCIM lightly tested; tenant isolation
needs real infra (Vault/IAM) to exercise; "BMad method" language is aspirational (planning is
subtask-based); per-phase model routing has no GUI knob; **there is no zero-config local-only
first-run yet** (the quickstart still expects a Claude OAuth token).

**Verdict:** the core pipeline is production-grade for solo/team use; enterprise features are
"built, hardening." This is a real product, not a prototype — a major asset and a credibility lever.

## 2. Where we sit in the market (mid-2026)

The category is crowded and well-funded at the top (Cursor >$2B ARR, Factory.ai $1.5B, Devin,
Copilot agent, Codex, Claude Code, Jules) — all cloud/closed/per-seat. **Do not compete head-on
as "another agent."**

The self-host lane: completion/IDE assistants are crowded (Tabby ~33k★, Continue ~31k★, Refact
~3k★) but they are not spec-first review-gated pipelines. OpenHands (~75k★, MIT, VPC+K8s+RBAC;
Enterprise launched May 2026) is closest on autonomy+K8s but lacks the compliance-evidence depth.
Qodo (closed, air-gap+SOC2) is the closest on the reliability/compliance thesis.

**Our place — the open quadrant:** no competitor found combines all three of (a) full autonomous
spec→plan→code→QA review-gated pipeline, (b) true self-hostable/air-gap-capable OSS, and (c) deep
compliance-evidence infra (hash-chained signed audit anchors, per-tenant K8s isolation, SOC2/ISO
evidence, gVisor, OTel). **The moat is the *combination*, not any single feature** — every piece
exists somewhere. Defensibility = integration + being first to own the story. The window is
eroding (OpenHands Enterprise, Qodo) — prioritize launch/positioning over more depth.

**Star calibration:** general-purpose agents reach 40–75k★; the compliance self-host segment tops
lower (Tabby ~33k, Refact ~3k). Honest ladder: ~1–3k = nascent · ~5–15k = real traction · ~10–30k
= top-tier for this niche. Aim the 6–12-month target at 5–15k, stretch 30k.

## 3. Do we solve a real need? (Jobs-to-be-done)

- **Platform/security engineer (buyer-champion):** "When my org bans sending source to external
  AI, I need an autonomous coding capability I can run inside my perimeter with SSO, audit, and
  isolation — so I can say yes to AI dev without failing the next audit."
- **Developer (daily user):** "When I hand work to an agent, I need to trust and review what it did
  — a spec, a plan, a diff, a QA pass — not babysit a black box or clean up vibe-code."

Both are real, under-served *together*, and quantified: 96% of devs don't fully trust AI output
but only 48% verify it; review effort up ~35%; 74% of orgs can't show provenance for AI code;
40–62% of AI-generated code carries vulnerabilities. **The need is real and sharpening.** The risk
is discovery + proof, not absence of need.

## 4. Positioning statement

> **AIFactory is the open-source, self-hostable AI software engineer for teams that can't send
> their code to the cloud and can't trust an unsupervised agent. Spec-first, fully auditable, runs
> in your own Kubernetes — no vendor lock-in.**

Anti-positioning: not a vibe-coding toy; not a cloud SaaS that ingests your repo; not single-model
lock-in; not an unsupervised black box.

## 5. Open-core line — free vs. paid

- **FREE / OSS (MIT — the adoption core):** full spec→plan→code→QA pipeline, web UI, all providers,
  git worktree isolation, single-tenant self-host, GitHub integration, CLI, MCP.
- **PAID / Enterprise Edition (commercial license, later):** multi-tenant isolation, SAML/SCIM,
  signed audit anchors + SOC2/ISO evidence export, OPA policy packs, priority support / SLAs,
  hardened Helm + reference architectures.
- **Licensing note:** for clean open-core, consider core = MIT (max adoption) and enterprise
  modules under a separate commercial/BSL-style license rather than blanket "MIT OR GPL-3.0."
  Decide deliberately; document a clear contribution/CLA stance.

## 6. The journey we're selling

Discover (blog/HN/community) → Try (`helm install` / docker-compose, first real task end-to-end in
<30 min) → Trust (review the audit trail + isolation; it passes the security bar) → Champion
(bring it to the platform/security team) → Expand (team needs multi-tenant/SSO/audit-export →
enterprise edition). The marketing job is to make Discover→Try→Trust effortless; the product job is
to make "<30 min to first audited build, self-hosted" literally true.

## 7. Gaps — prioritized for a solo founder

**P0 (adoption blockers):** frictionless first-run (one-command self-host + local-model, zero-key,
"first build in 10 minutes"); the proof artifact (2–3 min demo showing spec→plan→diff→QA→audit
log); README/landing/docs that lead with the contrarian governance angle. *(Docs/landing done in
this change; first-run + demo are engineering work.)*

**P1 (credibility):** make the audit trail visible/demoable in the UI; honest capability matrix;
finish/verify SAML/SCIM enough to claim truthfully; harden the Helm happy path.

**P2 (depth, later):** per-phase model-routing GUI (cost story); make Graphiti a real
differentiator or de-emphasize it; drop/rebrand "BMad method" until real.

**Discipline:** every P0 serves discovery + first-run + proof. Resist enterprise depth until demand pulls.

## 8. Go-to-market & marketing (channels available: blog, social, community, conference)

- **Content (top channel):** opinionated technical essays — "Why we can't use Cursor at a bank,"
  "Auditable autonomous coding," "Running an AI software engineer air-gapped," "Spec-first vs
  vibe-coding." Each ends with a self-host quickstart.
- **Launch surfaces:** Show HN, r/selfhosted, r/devops, r/LocalLLaMA (Ollama angle), Lobsters —
  coordinated with the demo + clean README.
- **Home communities first:** NixOS/DevOps/platform/self-hosting — seed for honest feedback before
  the big launch.
- **Conferences:** a talk on governance/auditability of AI agents — positions the founder as the
  thought leader on the serious side of the space.
- **GitHub hygiene as marketing:** crisp README, GIF, diagram, good-first-issues, fast response,
  public roadmap.
- **Sequencing:** seed → polish P0 → coordinated public launch → sustain (weekly content cadence).

## 9. Roadmap recommendation

- **Next 90 days (traction sprint):** ship P0 (one-command self-host, local-model path, demo,
  README/landing). Seed home communities. One flagship blog post. Coordinated Show HN.
- **6–12 months:** weekly content cadence; convert stars → contributors; land 1–3 *named*
  self-host deployments as proof; finish P1. Keep enterprise edition latent until pulled.
- **Monetization trigger & shape:** when ≥1 regulated org asks to pay for support / multi-tenant /
  audit export, stand up the enterprise edition + commercial license. Model = open-core + **annual
  enterprise license + support contract**, NOT usage/ACU metering (regulated buyers prefer fixed
  annual; reference floors: Sourcegraph Enterprise ~$16K/yr, Tabby/Continue enterprise tiers).

## 10. Risks & failure modes

- Niche too narrow for the "community traction" North Star → use the niche as a *hook*; the Ollama
  / local-LLM story widens the funnel.
- Solo bandwidth vs. enterprise surface area → defer enterprise depth; let demand pull.
- Trust gap on a young project → candor (capability matrix), visible audit trail, fast response,
  founder credibility (platform engineer).
- "Built a lot, shipped to no one" → the entire P0 is discovery + first-run + proof.
- Provider/SDK churn → multi-provider abstraction is the hedge; keep it genuinely neutral.
- Window closing → OpenHands Enterprise + Qodo are the two to watch; first-mover on the *narrative*
  matters more than feature completeness.

## 11. North Star & metrics

- **Primary:** GitHub stars + contributors + "on the map" signal (HN front page, inbound mentions).
- **Leading indicators:** self-host installs, time-to-first-build, demo-video views, blog→repo
  conversion, Discord/issue activity.
- **Proof metric:** number of named self-host deployments (the bridge to monetization).
