---
title: Contributing
sidebar_position: 4
---

# Contributing

## Branching model

- **`dev`** is the working branch. Feature work and PRs target `dev`.
- **`main`** is a release branch. It only receives promotion merges from `dev` (the `release: vX.Y.Z` commits you see).
- Do **not** branch new feature work from `main` — always branch from `origin/dev`.

```bash
git fetch origin
git checkout -b feat/my-feature origin/dev
# ... work ...
git commit -s -m "feat: my feature"   # -s for DCO sign-off
git push -u origin feat/my-feature
gh pr create --base dev
```

## Tests

```bash
# Backend unit tests
apps/backend/.venv/bin/pytest tests/ -m "not slow"

# Frontend typecheck
npm -w apps/frontend-web run typecheck

# Postgres acceptance (needs docker)
apps/backend/.venv/bin/pytest tests/postgres/ -m postgres -v
```

CI runs all of these on every PR. Don't ship red.

## Style

- Python: `ruff check apps/backend tests` must pass
- TypeScript: ESLint + Prettier (no custom rules; defaults work)
- Commits follow Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`)
- DCO sign-off required (`git commit -s`)

## What we welcome

- Bug fixes with a regression test
- New provider integrations (open an issue first to align on the abstraction)
- Documentation improvements
- Performance work backed by a benchmark

## What we'll redirect

- Big architectural pivots — open an issue and let's design first
- Adding new top-level features — open an issue, we'll roadmap it
- Adding telemetry or analytics — we don't, by policy

## Code of Conduct

See [CODE_OF_CONDUCT.md](https://github.com/olafkfreund/AIFactory/blob/main/CODE_OF_CONDUCT.md). Be kind, assume good faith, escalate to maintainers if you encounter problems.
