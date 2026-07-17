# error-handling

> Source: curated best practices | 2026

---

# Error Handling - fail fast, fail loud, never lose data silently

The worst error is the one that gets swallowed: a bare `except` that hides a bug for six months, a returned `null` nobody checks, a retry loop that hammers a dead service. Robust code decides, for every failure, whether to handle it, translate it, or propagate it — and it never pretends a failure did not happen. Fail fast on programmer errors (bad state, impossible input) so they surface in development; handle expected failures (network blip, missing file) deliberately with context, retries where safe, and idempotency so a retry cannot corrupt state.

## When to Activate

Use when code can fail — which is almost all non-trivial code:
- any I/O: network calls, file access, database queries, subprocess
- parsing or validating external input
- writing catch/except/rescue blocks or defining error types
- operations that may need retries, timeouts, or must be safe to repeat
- reviewing code with empty catch blocks or ignored return values

## Principles and Practices

**Fail fast on the impossible.** If an argument violates a precondition or the program reaches a state that should never happen, raise immediately — do not limp forward on bad data. A loud crash in development is a gift; a silent corruption in production is a disaster.

```python
def withdraw(account, amount):
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    ...
```

**No silent excepts.** The single worst pattern in error handling:

```python
try:
    result = risky()
except Exception:
    pass          # WRONG: the bug is now invisible
```

If you catch, do one of: handle it meaningfully, log it with context and re-raise, or translate it to a domain error. Catching `Exception`/`Throwable` broadly is almost always wrong — catch the specific type you can actually handle. Never catch and continue as if nothing happened unless you can articulate exactly why that is correct (and leave a comment).

**Typed / domain errors.** Distinguish error kinds so callers can react differently. A `NotFoundError`, `ValidationError`, and `TimeoutError` each imply a different response (404 vs 400 vs 503). Wrap low-level errors in domain terms at layer boundaries so callers do not depend on the storage engine's exception classes.

```go
if err != nil {
    return fmt.Errorf("loading user %d: %w", id, err)  // wrap with context, keep the chain
}
```

**Add context as it propagates.** An error message that reaches the log should say what was being attempted with which inputs: `"loading user 42: connection refused"` beats `"connection refused"`. Use error wrapping (`%w` in Go, exception chaining / `raise ... from e` in Python, `cause` in JS) so the original stack is preserved. Do not log-and-rethrow at every level — that produces duplicate noise; add context once where you have it and let it propagate.

**Retries with backoff — only for transient, idempotent operations.** Retrying a timeout or 503 makes sense; retrying a 400 or "duplicate key" does not — it will fail forever and waste resources. Use exponential backoff with jitter and a cap on attempts, so a struggling service is not stampeded.

```python
for attempt in range(5):
    try:
        return call()
    except TransientError:
        if attempt == 4: raise
        sleep(min(2 ** attempt, 30) + random.random())   # backoff + jitter, capped
```

**Idempotency makes retries safe.** If an operation can be retried (by your code, a client, or a message queue redelivery), it must be safe to run twice. Use idempotency keys, upserts, or "create if not exists" so a duplicate delivery does not double-charge a card or double-send an email. Design for at-least-once delivery — exactly-once is a myth at the transport layer.

**Clean up on failure.** Release resources deterministically: `with`/`defer`/`try-finally`/RAII. A failure mid-operation must not leak a file handle, connection, or lock. For multi-step operations that partially succeed, either make the whole thing a transaction or define compensating actions.

**Timeouts on everything external.** A network call without a timeout can hang forever and exhaust your thread/connection pool, turning one slow dependency into a full outage. Set explicit, sane timeouts on every outbound call.

**Errors are values at the boundary.** At an API/UI boundary, translate exceptions into structured responses (status code + safe message + optional error id) and never leak internal details. Log the full detail server-side keyed by that id.

## Anti-patterns

- `except: pass` / `catch (e) {}` — the silent swallow that hides bugs.
- Catching `Exception`/`Throwable` broadly when you can only handle one kind.
- Returning `null`/`-1`/`""` to signal failure where a caller will forget to check — raise or return a typed result instead.
- Retrying non-idempotent or non-transient operations (double charges, infinite retry of a 400).
- Retry loops with no backoff, no jitter, and no attempt cap — a self-inflicted DDoS.
- Logging the same error at every stack level, producing noise with no new context.
- No timeout on network/DB calls — one hung dependency takes down the service.
- Leaking stack traces or internal error text to end users.
- Swallowing an error and returning partial/empty data as if the call succeeded.
