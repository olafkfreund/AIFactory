# AIFactory — Launch Kit

Collateral for the open-source positioning launch. Derived from the GTM strategy
([`docs/plans/2026-05-30-oss-gtm-strategy.md`](../docs/plans/2026-05-30-oss-gtm-strategy.md)).

**Positioning in one line:** *The open-source AI software engineer you can self-host and audit —
spec-first, multi-model, runs in your own cluster.*

## Contents

| File | What it is | Where it goes |
|------|------------|---------------|
| [`SHOW-HN.md`](./SHOW-HN.md) | "Show HN" submission (title + body) | news.ycombinator.com |
| [`reddit-and-social.md`](./reddit-and-social.md) | r/selfhosted, r/devops, r/LocalLLaMA, LinkedIn, X variants | each community |
| [`blog-why-we-cant-use-cursor-at-a-bank.md`](./blog-why-we-cant-use-cursor-at-a-bank.md) | Flagship blog post draft | your blog (Jekyll) |
| [`demo-shot-list.md`](./demo-shot-list.md) | 2–3 min demo video script + shot list | the proof artifact |
| [`README-hero-proposal.md`](./README-hero-proposal.md) | Proposed repositioned repo README hero | a deliberate `README.md` diff |

## ⚠️ Truth gate — make these TRUE before you launch

The copy here is written to be honest, but two headline promises are **aspirational today** and
will sink a launch if they bounce front-page traffic. Treat these as the real P0 engineering work
that must land *before* posting:

1. **Frictionless first-run.** The copy implies "self-host and run a task fast, on your own model."
   Today the quickstart still expects a Claude OAuth token (`claude setup-token`) and there is **no
   zero-config local-only path**. Before launch, ship either:
   - `docker compose up` → working portal → first task on a **local Ollama model with no external
     API key**, *or*
   - if that's not ready, **soften the copy** to "bring your own Claude seat (or point it at a local
     model)" and drop any "no API key" phrasing. Do not claim what isn't true.
2. **The 2–3 min demo video.** Every asset links to it. It is the single highest-leverage artifact
   (more than any copy). Record it from `demo-shot-list.md`: spec → plan → diff → QA → **visible
   audit log**. No video, no launch.

Also verify before posting: the repo README leads with the new positioning (see
`README-hero-proposal.md`); "BMad method" language is removed or marked aspirational; the
capability matrix (what's solid vs. beta) is published.

## Suggested launch sequence

1. **Seed (week 1–2):** post the blog draft; share in your home communities (NixOS / DevOps /
   self-hosted) for honest feedback. Fix the rough edges they find.
2. **Polish (week 2–3):** land the P0 first-run + record the demo. Repoint README. Add
   good-first-issue labels.
3. **Launch day (coordinated):** publish blog post → submit Show HN → cross-post the Reddit
   variants (space them out; lead r/LocalLLaMA with the Ollama angle). Be at the keyboard all day
   to answer every comment fast — responsiveness is what keeps you on the front page.
4. **Sustain:** weekly content cadence; ship-in-public changelog; convert stars into contributors.

## North Star

Community traction (stars, contributors, "on the map"). Honest 6–12-month target: **5–15k stars**,
stretch 30k. Watch leading indicators: self-host installs, time-to-first-build, demo views,
blog→repo conversion.
