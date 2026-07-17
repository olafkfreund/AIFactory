# rust

> Source: curated best practices | 2026

---

# Rust - Safe, zero-cost systems and application code

This skill equips the coder to write idiomatic Rust (2021 edition, stable toolchain) that leans on the ownership/borrow model, models errors with `Result` and the `?` operator, and prefers iterators and pattern matching over manual loops. It assumes a Cargo project, `rustfmt`, `clippy` at deny-warnings, `thiserror`/`anyhow` for errors, and built-in `#[test]` plus `cargo test`.

## When to Activate

Use when the task involves Rust:
- Writing or modifying `.rs` files, crates, or workspaces
- Building CLIs, services, embedded, or performance-critical libraries in Rust
- Anything referencing `Cargo.toml`, `cargo test`, `clippy`, ownership, lifetimes, `async`

## Idioms and Best Practices

**Project layout** (Cargo standard):
```
mycrate/
  Cargo.toml
  src/lib.rs        // library root
  src/main.rs       // binary root
  src/store.rs
  tests/integration.rs
```

**Errors: `Result` + `?`, never `unwrap()` in library/production paths.** Use `thiserror` for library error enums, `anyhow` for application top-level:
```rust
use thiserror::Error;

#[derive(Error, Debug)]
pub enum ConfigError {
    #[error("io error reading {path}")]
    Io { path: String, #[source] source: std::io::Error },
    #[error("invalid value: {0}")]
    Invalid(String),
}

fn load(path: &str) -> Result<Config, ConfigError> {
    let raw = std::fs::read_to_string(path)
        .map_err(|e| ConfigError::Io { path: path.into(), source: e })?;
    parse(&raw)
}
```
`unwrap()`/`expect()` are acceptable only in tests, `main`, or when a panic is genuinely the right response (with a message explaining the invariant).

**Ownership: borrow by default.** Take `&str` not `String`, `&[T]` not `Vec<T>`, in function parameters. Return owned values. Clone deliberately, not to appease the borrow checker - restructure instead.

**Model with enums and match.** Rust's enums are sum types; use them for state machines and options:
```rust
enum State { Idle, Running { pid: u32 }, Done(i32) }

match state {
    State::Idle => start(),
    State::Running { pid } => stop(pid),
    State::Done(code) => report(code),
}
```
`match` is exhaustive - the compiler catches missed variants.

**Iterators over index loops.** Lazy, composable, often faster:
```rust
let total: u64 = items.iter().filter(|i| i.active).map(|i| i.amount).sum();
let names: Vec<_> = users.iter().map(|u| u.name.clone()).collect();
```
Use `Option`/`Result` combinators (`map`, `and_then`, `ok_or`, `unwrap_or_default`) instead of manual matching where it reads cleaner.

**`Option<T>` for absence, never sentinels or null.** Use `if let`/`let else` for ergonomic unwrapping:
```rust
let Some(user) = find(id) else {
    return Err(ConfigError::Invalid("no user".into()));
};
```

**Traits for shared behavior; keep them focused.** Derive common ones: `#[derive(Debug, Clone, PartialEq)]`. Prefer generics with trait bounds over `dyn Trait` unless you need runtime polymorphism.

**Concurrency:** the type system enforces safety. Use `std::thread` with `Arc<Mutex<T>>` for shared state, channels (`std::sync::mpsc`) for message passing. For async, use `tokio` with `.await`; don't block the async runtime with sync I/O. `Send`/`Sync` are checked for you.

**Testing** (unit tests live in the module):
```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_valid() {
        assert_eq!(parse("42").unwrap(), 42);
    }

    #[test]
    fn rejects_junk() {
        assert!(parse("x").is_err());
    }
}
```
Integration tests go in `tests/`. Run `cargo test`.

**Tooling:** `cargo fmt` (rustfmt, canonical), `cargo clippy -- -D warnings` (fix every lint), `cargo build --release` for optimized builds. Prefer `?` and clippy suggestions - they encode idiom.

## Anti-patterns

- `.unwrap()`/`.expect()` on `Result`/`Option` in production code paths.
- Cloning everywhere to escape the borrow checker instead of borrowing.
- `unsafe` without a clear, documented invariant and a safe wrapper.
- `Rc<RefCell<T>>` graphs where ownership could be modeled linearly.
- Returning `String`/`Vec` params where `&str`/`&[T]` suffice.
- Reaching for `dyn` trait objects when generics fit.
- Ignoring clippy warnings or disabling them wholesale.
- Blocking calls inside `async fn` running on a shared runtime.
