# Show HN submission

> ⚠️ Before posting, clear the truth gate in [`README.md`](./README.md): the demo video must
> exist, and either the zero-config local first-run is real or the "local model" line below is
> softened to match reality.

## Title

`Show HN: AIFactory – open-source autonomous coding you can self-host and audit`

(Alternative: `Show HN: A self-hostable, auditable AI software engineer (spec → plan → code → QA)`)

## URL

`https://github.com/olafkfreund/AIFactory`

## Body (first comment)

Hi HN. I'm a platform engineer and I built AIFactory because I kept hitting the same wall: teams
that legally can't send their source to a cloud AI, and engineers who don't trust an agent's output
enough to merge it unread. The unease is well-founded — Sonar found 96% of developers don't fully
trust AI-generated code, but only 48% verify it, and ~74% of orgs can't produce security provenance
for AI code.

So instead of another "watch it write code" demo, AIFactory is built around *verifiability*. Every
task becomes a written spec, then a plan you approve, then code in an isolated git worktree, then a
QA pass against the acceptance criteria. Every action lands in a hash-chained audit log. You bring
your own model — Claude, OpenAI, Gemini, or a local Ollama / OpenAI-compatible endpoint — and you
run the whole thing in your own infra (docker-compose on a laptop, Helm on Kubernetes).

What's solid today: the spec → plan → code → QA pipeline, the web UI (Kanban, editor, terminals,
live agent console, GitHub PR review), multi-provider routing, per-task git-worktree isolation, and
the audit chain. What's still beta and where I'd genuinely value eyes: multi-tenant isolation and
SAML/SCIM for larger orgs.

It's MIT-licensed and I'm building it solo, full-time. Two things I'd love feedback on:

1. Does the spec-first / review-gated model match how you'd actually want to delegate to an agent —
   or is it too much ceremony?
2. For those of you in regulated shops: what would it take to clear your security bar for something
   like this?

Repo + 2-min demo in the README. Thanks for taking a look.

## Notes for the poster

- Post in the **morning US Pacific** on a weekday for best visibility.
- The body goes as your **first comment**, not the submission text (HN convention for Show HN).
- Answer every top-level comment within minutes for the first few hours.
- Don't get defensive about "isn't this just X?" — lean into the *combination* (autonomous +
  self-host + audit) that the comparison tools don't have.
