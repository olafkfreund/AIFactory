# testing

> Source: curated best practices | 2026

---

# Testing - write tests that catch real bugs and never lie

Good tests give you the confidence to change code fast. They fail for exactly one reason, they run the same way every time, and they exercise behavior a user cares about rather than the shape of the implementation. The goal is not a coverage number; it is a suite you trust enough to refactor behind. Cheap, fast, deterministic tests at the bottom; a few slow end-to-end tests at the top; and every test readable enough that a failure message tells you what broke without opening the code.

## When to Activate

Use when writing, reviewing, or changing any code with logic worth protecting:
- adding a function, branch, loop, parser, or money/security path
- fixing a bug (write the failing test first, then fix)
- refactoring and you need a safety net
- reviewing a PR that changes behavior but adds no tests
- a suite is flaky, slow, or passes when the code is obviously broken

## Principles and Practices

**Test pyramid.** Many fast unit tests, fewer integration tests, very few end-to-end tests. Unit tests catch logic bugs in milliseconds; e2e tests catch wiring bugs but are slow and brittle. If your suite is an ice-cream cone (mostly e2e), it is slow and flaky. Invert it.

**Arrange-Act-Assert.** Every test has three visible phases: set up inputs, run the thing once, assert the result. Keep them in that order with a blank line between. If "Act" is more than one call, you are probably testing two things.

```python
def test_discount_applies_to_subtotal():
    cart = Cart(items=[Item(price=100), Item(price=50)])   # Arrange
    total = cart.total(discount=0.10)                       # Act
    assert total == 135                                     # Assert
```

**One behavior per test.** Not literally one `assert`, but one reason to fail. Asserting a returned object's three fields is fine; asserting "discount works AND tax works AND shipping works" is three tests wearing a trench coat. The test name states the behavior: `test_expired_token_is_rejected`, not `test_auth`.

**Table-driven tests** for the same logic over many inputs. One test body, a list of cases. Adding a case is one line, and each case reports its own name on failure.

```go
func TestParse(t *testing.T) {
    cases := []struct{ name, in string; want int; wantErr bool }{
        {"simple", "42", 42, false},
        {"negative", "-7", -7, false},
        {"garbage", "abc", 0, true},
    }
    for _, c := range cases {
        t.Run(c.name, func(t *testing.T) {
            got, err := Parse(c.in)
            if (err != nil) != c.wantErr { t.Fatalf("err = %v, wantErr %v", err, c.wantErr) }
            if got != c.want { t.Errorf("got %d, want %d", got, c.want) }
        })
    }
}
```

**Determinism is non-negotiable.** A test that passes 99% of the time is worse than no test — it trains the team to ignore red. Sources of flake and their fixes:
- **Time:** inject a clock or freeze it; never assert on `now()`.
- **Randomness:** seed it, or inject the value.
- **Sleeps:** never `sleep(2)` and hope. Poll for the condition with a timeout, or use test hooks/fakes that complete synchronously.
- **Ordering:** never depend on dict/map iteration order or test execution order. Each test sets up its own state.
- **Shared state:** fresh fixtures per test; roll back the DB or use a transaction per test. Torn writes between parallel tests are a classic heisenbug.

**Fixtures and mocks — use sparingly.** Prefer real objects and in-memory fakes over mocks. Mock at the edges you do not own (network, clock, payment gateway), not your own internal classes. Over-mocking couples the test to the implementation, so a refactor breaks 40 green tests that tested nothing real. A fixture should set up the minimum state the test needs; giant shared fixtures are a smell.

**Test behavior, not implementation.** Assert on outputs and observable side effects, not on which private method was called how many times. If renaming an internal helper breaks a test, the test was measuring the wrong thing. This is what lets you refactor freely.

**Coverage that matters.** 100% line coverage with no assertions proves nothing. Aim to cover branches and edge cases: empty input, boundary values (0, 1, max, off-by-one), null/None, error paths, and the one weird case the bug report named. A regression test for every fixed bug — the bug proves that path was untested.

**Fast by default.** Unit tests run in milliseconds and on every save. If the suite takes minutes, people stop running it. Put slow tests behind a marker/tag so the fast loop stays fast.

## Anti-patterns

- Asserting nothing (a test that only checks "it did not throw" — say so explicitly or add a real assertion).
- `sleep()` to wait for async work — poll for the condition instead.
- Tests that depend on run order or leak state into each other.
- Mocking the thing under test, or mocking so much the test only verifies the mocks.
- Snapshot tests blindly re-approved on every change — they rot into rubber stamps.
- One giant test that does ten things; when it fails you learn nothing.
- Chasing a coverage % by testing getters and generated code instead of logic.
- Deleting or `@skip`-ing a failing test to make CI green instead of fixing the cause.
