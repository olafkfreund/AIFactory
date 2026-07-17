# clean-code

> Source: curated best practices | 2026

---

# Clean Code - optimize for the next person to read it

Code is read far more often than it is written, so the highest-value quality is clarity. Clean code is not clever code — it is code where a reader can predict what a function does from its name, follow the logic without holding ten things in their head, and change one behavior without touching twenty files. The disciplines are unglamorous: name things honestly, keep functions small and single-purpose, remove what is not used, factor out genuine duplication (but not coincidental duplication), and comment the "why" that the code cannot express. Boring, obvious code is a feature.

## When to Activate

Use when writing or reviewing any code, broadly:
- naming a variable, function, class, module, or file
- a function grows past a screen, or nests three levels deep
- you notice copy-pasted logic, dead code, or commented-out blocks
- reviewing a PR for readability and maintainability
- refactoring before adding a feature to messy code

## Principles and Practices

**Naming is the highest-leverage decision.** A name should say what the thing is or does with no comment needed. Length should match scope — a loop index `i` is fine; a module-level export needs a full, descriptive name.

```python
# WRONG                          # RIGHT
d = get()                        active_users = fetch_active_users()
def proc(x): ...                 def calculate_tax(order): ...
flag = True                      is_email_verified = True
```

Avoid abbreviations that are not universal (`usr`, `calc`, `tmp2`), single letters outside tight loops, and names that lie (a `get_user` that also writes to the DB). Booleans read as predicates (`is_`, `has_`, `can_`). Functions are verbs; classes and values are nouns.

**Small functions that do one thing.** A function should do one thing at one level of abstraction. If you cannot describe it without "and", split it. Prefer functions that fit on a screen. Deep nesting is a smell — invert conditions and return early (guard clauses) instead of pyramids of `if`.

```python
# guard clauses flatten the logic and handle edge cases up front
def charge(order):
    if order is None: raise ValueError("no order")
    if order.paid: return order
    if order.total <= 0: raise ValueError("nothing to charge")
    return gateway.charge(order.total)   # the one real action, unindented
```

**Few arguments.** Zero to three is comfortable; four-plus is a sign of a missing struct/object. Avoid boolean flag parameters that make the function do two different things (`render(data, True)` — true what?) — split into two named functions.

**DRY, but not premature abstraction.** Extract logic that is genuinely the same rule appearing in multiple places — a bug fix should touch one spot, not five. But do NOT abstract code that merely looks similar today and will diverge tomorrow; a shared helper coupling two things that should evolve independently is worse than the duplication. The rule of thumb: duplicate twice, extract on the third occurrence when the pattern is proven. A wrong abstraction is more expensive to unwind than repeated code.

**Comments explain WHY, not WHAT.** The code already says what it does. A comment earns its place by explaining intent, a non-obvious constraint, a workaround, or a link to the reason.

```python
# WRONG: restating the code
i += 1  # increment i
# RIGHT: explaining the why
# Stripe rounds half-up; we must match or reconciliation drifts by a cent.
amount = round_half_up(total)
```

Delete commented-out code — version control remembers it. A comment that has drifted out of sync with the code is worse than none; update or delete it.

**No dead code.** Unused functions, unreachable branches, variables assigned and never read, imports for things no longer used, feature flags for shipped features — delete them. Dead code is a tax: readers must figure out whether it matters, and it hides real logic. Your VCS is the archive.

**Consistency over personal preference.** Match the file's existing style, the project's formatter, and its conventions even if you would do it differently. A consistent codebase is navigable; a patchwork of styles is friction on every read. Run the auto-formatter and linter; do not hand-argue whitespace.

**Keep related things close, unrelated things apart.** A function should live near what it uses; a module should have one responsibility. If a file does five unrelated jobs, split it. High cohesion within a unit, low coupling between units.

**Prefer immutability and pure functions where practical.** Functions that take inputs and return outputs without hidden side effects are easier to test, reason about, and reuse. Isolate the side-effecting I/O at the edges.

## Anti-patterns

- Names that lie, abbreviate cryptically, or need a comment to decode.
- Functions that do several things; 200-line methods; five levels of nesting.
- Boolean flag parameters that fork behavior — split into named functions.
- Extracting an abstraction from two things that merely look alike and will diverge.
- Comments that restate the code, or stale comments contradicting it.
- Commented-out code and dead functions left "just in case".
- Reformatting a whole file in a feature PR, drowning the real diff.
- Deep inheritance or clever metaprogramming where a plain function would do.
- Inconsistent style within a codebase; ignoring the project formatter/linter.
