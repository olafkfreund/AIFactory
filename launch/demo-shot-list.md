# Demo video — script & shot list

**Goal:** In ≤3 minutes, prove the one thing competitors can't show: an autonomous coding run that
is **self-hosted, review-gated, and fully auditable.** The payoff shot is the **audit log** — most
agent demos can't show that.

**Format:** screen recording, no face needed. Captions over voice (or light voiceover). Export as
MP4 + a short GIF of the best 8 seconds for the README/social.

**Setup before recording:**
- A clean self-hosted instance (docker-compose or your cluster) — show it's *yours*, not a cloud.
- A small, real sample repo with one believable task (e.g. "add input validation + tests to the
  `/signup` endpoint"). Avoid toy tasks; pick something a reviewer would actually scrutinize.
- Pre-warm so there's no long dead air; you'll trim, but keep it honest (note time compression).

---

## Shot list

**0:00–0:15 — The hook (problem framing).**
Caption: *"AI that writes code is easy. AI you can audit and run in your own infra isn't."*
Show the AIFactory portal running on `localhost` / your cluster. Caption: *"Self-hosted. Your code
never leaves here."*

**0:15–0:35 — The task → spec.**
Create a task from the sample issue. Show the **spec** it generates — the acceptance criteria.
Caption: *"Every run starts from a written spec, not a vibe."*

**0:35–1:05 — The plan gate.**
Show the implementation plan. Pause on the **Approve Plan** gate; click approve.
Caption: *"You approve the plan before a line of code is written."*

**1:05–1:45 — The build, isolated.**
Show the live agent console working; mention/show it's in an isolated git worktree.
Caption: *"Runs in an isolated worktree — nothing touches your branch until you say so."*
Show the model in use, and that it can be local. Caption: *"Your model, your choice — incl. local."*

**1:45–2:20 — The diff + QA gate.**
Show the produced diff and the **QA report** checking it against the spec's acceptance criteria.
Caption: *"A QA pass against the spec — then you review the diff."*

**2:20–2:50 — The payoff: the audit trail.**
Open the **hash-chained audit log** showing the recorded actions for this task. This is the shot.
Caption: *"Every action recorded. Provenance your auditor actually asks for."*

**2:50–3:00 — Close / CTA.**
Caption: *"Open-source. Self-hosted. Auditable. github.com/olafkfreund/AIFactory"*

---

## Truthfulness rules (non-negotiable)

- If a step isn't real yet (e.g. the audit-log UI isn't built), **don't fake it** — either build the
  view first or cut that beat and adjust the copy everywhere to match.
- If you compress time, say so in a caption ("timeline compressed").
- Use a real model and a real repo. Reviewers can smell a staged demo, and getting caught once on a
  launch costs more trust than the demo earns.

## Reuse

- 8–12s GIF of the plan-gate → audit-log beats for the README hero and social.
- Stills of the spec, plan, diff, QA report, and audit log for the blog post.
