# Feature Architecture Standard

> Status: Active · Owner: AIFactory maintainers · Issue: [#261](https://github.com/olafkfreund/AIFactory/issues/261)
> Enforcement: `ruff check apps/backend tests` (the existing CI step) — no extra tooling.

This standard describes how AIFactory structures a *feature* so that its
**business logic stays isolated from transport and runtime concerns**. It is a
pragmatic adaptation of ports-and-adapters / clean architecture to *this*
codebase — Python backend, FastAPI web-server, React frontend — not a generic
restatement of the pattern.

The standard is **lint-enforced**: at least one import boundary is a hard CI
gate today (see [Enforcement](#enforcement)). The goal is that the rules grow
incrementally, one verifiable boundary at a time, rather than as an aspirational
document nobody runs.

---

## 1. Goals

1. **Pure logic is reusable across runtimes.** A piece of domain/application
   logic must be loadable by *any* process — the CLI build pipeline, the
   FastAPI web-server, a unit test — without dragging in heavyweight,
   environment-specific dependencies (the Claude Agent SDK, OAuth, network
   clients).
2. **Dependencies point inward.** Transport and adapter code may import domain
   logic; domain logic must never import transport. Cross-cutting wiring is done
   by **dependency injection** at the edge, not by inner layers reaching out.
3. **Boundaries are mechanically checked.** "Don't import X from Y" is worth
   nothing unless CI fails when violated. Every boundary in this document is
   either enforced by `ruff` today or explicitly marked as *aspirational, not
   yet enforced*.

### Why this matters here (the motivating pain)

PR [#260](https://github.com/olafkfreund/AIFactory/pull/260) needed the
web-server to call backend review-cycle logic (`qa.review_redrive`). Importing
the `qa` *package* runs `qa/__init__.py`, which pulls in the Claude Agent SDK
and other agent dependencies — heavy, and unavailable without OAuth in a plain
web-server process. The workaround was an `importlib` hack in
`apps/web-server/server/services/review_redrive_service.py` that loads the two
needed modules *by file path* without executing `qa/__init__`.

That hack only works **because `qa/review_cycle.py` and `qa/review_redrive.py`
are pure stdlib leaf modules with no SDK import.** If someone later adds
`from claude_agent_sdk import ...` to one of them, the web-server breaks at
runtime — far from the change that caused it. This standard turns that implicit,
fragile contract into an explicit, lint-enforced one.

---

## 2. Layers and the canonical layout

AIFactory does not (and should not) move every module into rigid `domain/` /
`adapters/` folders overnight. Instead we classify *modules* into roles. A
feature is well-formed when each module sits cleanly in one role and only
imports "inward".

| Role | What it is | Lives (today) | May import | May NOT import |
|------|------------|---------------|------------|----------------|
| **Domain / application logic** ("pure-logic leaf") | Decisions, state machines, pure transforms. Deterministic, unit-testable with fakes. | `apps/backend/qa/review_cycle.py`, `apps/backend/qa/review_redrive.py` | stdlib, sibling pure-logic modules, typed protocols | the Claude Agent SDK, FastAPI/Starlette, web-server modules, live network clients |
| **Adapters** | Drive the outside world: run agent sessions, call the SDK/providers, hit the filesystem/network. | `apps/backend/qa/reviewer.py`, `qa/fixer.py`, `qa/loop.py`; `apps/backend/core/`, `providers/`, `agents/`, `runners/` | domain logic, the SDK, providers, MCP | — (this is where the heavy deps belong) |
| **Transport** | HTTP/WebSocket surface. Translates requests into calls on adapters/logic; owns no business rules. | `apps/web-server/server/routes/`, `services/` | domain logic, adapters via DI, pydantic models | — |
| **Frontend** | React UI. Talks only to the transport layer over REST/WS. | `apps/frontend-web/src/` | the web-server API | backend Python directly |

### Dependency direction

```
 React frontend ─▶ web-server (transport) ─▶ adapters ─▶ domain/application logic
                                  │                            ▲
                                  └────── dependency injection ─┘
        (transport passes concrete functions INTO pure logic;
         pure logic depends on the parameter/protocol, never the caller)
```

The arrows only point one way. The single allowed "outward" interaction is
**dependency injection**: a pure-logic function declares a parameter (e.g.
`enqueue`, `write_control`) and the transport layer passes the concrete
implementation in. `qa/review_redrive.py` is the worked example — it takes
`enqueue` and `write_control` as arguments rather than importing
`inbox_service`/`task_control` from the web-server.

---

## 3. Layer responsibilities

### Domain / application logic (pure-logic leaf)
- **Must** be importable by file with only the Python standard library available.
- **Must not** `import claude_agent_sdk` (directly or by importing an adapter
  that does). This is the **enforced** boundary.
- **Should not** import FastAPI/Starlette, the web-server package, or live
  network/provider clients (aspirational — not yet lint-enforced; see §5).
- Receives side-effecting collaborators via parameters/protocols (DI).

### Adapters
- The *only* place the Claude Agent SDK, providers, and MCP wiring belong.
- Translate between pure-logic data structures and external systems.
- May import domain logic; must not import transport.

### Transport (web-server)
- Owns request/response shapes (pydantic), auth, routing.
- Wires concrete collaborators into pure logic via DI.
- Holds **no** business rules — if a rule appears here, it belongs in a
  pure-logic module.

### Frontend
- Talks to the web-server API only. Never reaches into backend Python.

---

## 4. Enforcement

The boundary is enforced by **ruff**, using the rule the project already runs in
CI (`ruff check apps/backend tests`). **No new dependency and no CI-workflow
change** were needed.

**The enforced contract (today):**

> Pure-logic leaf modules in `apps/backend/qa/` must not import
> `claude_agent_sdk`.

**How it is expressed** (`ruff.toml`):

- `TID251` (flake8-tidy-imports `banned-api`) is added to `lint.select`.
- `claude_agent_sdk` is registered as a banned import under
  `[lint.flake8-tidy-imports.banned-api]`.
- The ban is then **exempted everywhere except the qa pure-logic leaf modules**,
  via `[lint.per-file-ignores]`. Adapter trees that legitimately use the SDK
  (`apps/backend/agents/`, `core/`, `providers/`, `runners/`, the web-server,
  tests, …) carry a `"TID251"` ignore. The agent-bearing qa adapters
  (`qa/reviewer.py`, `qa/fixer.py`, `qa/loop.py`, `qa/qa_loop.py`) are listed as
  **documented exceptions**.
- `qa/review_cycle.py` and `qa/review_redrive.py` are deliberately **not**
  exempted, so the ban is live for them.

**Result:** adding `from claude_agent_sdk import ...` to a qa leaf module fails
`ruff check` (TID251) — turning the #260 runtime-only contract into a CI gate.
To intentionally make a qa module SDK-dependent, a developer must consciously add
it to the exception list in `ruff.toml` (a reviewable, visible decision).

Run it locally:

```bash
apps/backend/.venv/bin/ruff check apps/backend tests
```

### Scope note / honest limitation
- `ruff`'s `banned-api` is a **static, direct-import** check. It catches a leaf
  module that *directly* imports `claude_agent_sdk`. It does **not** trace
  transitive imports (e.g. a leaf importing an adapter that imports the SDK).
  That is acceptable here: the importlib hack in #260 depends specifically on the
  leaf modules' *own* import lists staying SDK-free, which is exactly what this
  rule guards.
- The ban is scoped to `apps/backend/qa/` (the proven-painful boundary) rather
  than repo-wide, so it holds green on current `dev` without a sweeping refactor.

---

## 5. Roadmap for additional boundaries (not yet enforced)

These are the next boundaries to codify, each as a small, separately-verifiable
rule. They are **documented, not yet enforced** — do not assume CI checks them:

1. Domain logic must not import FastAPI/Starlette or the `apps/web-server`
   package.
2. Transport routes must not contain business rules (harder to lint; candidate
   for review checklist rather than ruff).
3. Extend the SDK ban to other pure-logic packages as they are identified.

When adding a boundary, follow the same recipe: pick a contract that **already
holds** on `dev`, express it as the narrowest ruff rule that bites, prove it
fails on an injected violation, and confirm `ruff check apps/backend tests`
stays green.

---

## 6. Pilot / reference implementation

The **QA review-cycle feature** is the designated reference implementation of
this standard:

| Module | Role | Conforms because |
|--------|------|------------------|
| `apps/backend/qa/review_cycle.py` | Domain logic | Pure stdlib state machine (`ReviewCycle`, transitions). No SDK, no web-server, no network. |
| `apps/backend/qa/review_redrive.py` | Application logic | Strike/escalation policy. Side effects (`enqueue`, `write_control`) are **injected as parameters**, never imported. |
| `apps/backend/qa/reviewer.py`, `qa/fixer.py`, `qa/loop.py` | Adapters | Run live agent sessions; the SDK lives here, by design. |
| `apps/web-server/server/services/review_redrive_service.py` | Transport wiring | Loads the leaf modules by file path and injects the web-server's `inbox_service.enqueue` / `task_control.write_control`. |

No refactor was required — these modules already conform; this standard
*codifies* the contract they were written to. New features should mirror this
shape: pure-logic core + injected side effects + a thin transport wiring layer.
