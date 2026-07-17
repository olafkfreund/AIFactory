# cpp

> Source: curated best practices | 2026

---

# C++ - Modern, RAII-driven, memory-safe systems code

This skill equips the coder to write modern C++20/23 that follows the C++ Core Guidelines: RAII for all resources, smart pointers over raw `new`/`delete`, value semantics and the rule of zero, the standard library and algorithms over hand-rolled loops, and `const`-correctness throughout. It assumes a CMake project, `clang-format`/`clang-tidy`, sanitizers in CI, and GoogleTest (or Catch2) for tests.

## When to Activate

Use when the task involves C++:
- Writing or modifying `.cpp`/`.hpp`/`.cc`/`.h` files, CMake projects
- Building performance-critical libraries, systems tools, or engines in C++
- Anything referencing `CMakeLists.txt`, `std::`, smart pointers, templates, RAII

## Idioms and Best Practices

**RAII for every resource** - memory, files, locks, sockets. Ownership lives in an object whose destructor releases it. Never manually pair `new`/`delete` or `lock`/`unlock`:
```cpp
{
    std::lock_guard<std::mutex> guard(mutex_);  // unlocks on scope exit
    std::ifstream file{path};                    // closes on scope exit
    // ...
}   // everything released here, even on exception
```

**Smart pointers express ownership:**
```cpp
auto widget = std::make_unique<Widget>(args);   // sole owner
std::shared_ptr<Config> cfg = std::make_shared<Config>();  // shared owner
```
`unique_ptr` by default; `shared_ptr` only when ownership is genuinely shared; raw pointers/references for non-owning access. Never `new`/`delete` in application code.

**Rule of zero:** design classes so the compiler-generated special members are correct - hold resources in RAII members (`std::string`, `std::vector`, `unique_ptr`) and write no destructor/copy/move at all. Only write them (rule of five) when managing a raw resource directly.

**Value semantics and const-correctness.** Pass cheap types by value, expensive read-only types by `const&`, sink parameters by value + `std::move`:
```cpp
void render(const Scene& scene);          // read-only, no copy
void store(std::string name) { data_ = std::move(name); }  // takes ownership
```
Mark member functions `const` when they don't mutate; mark locals `const` by default.

**Prefer algorithms and ranges over raw loops:**
```cpp
#include <algorithm>
#include <ranges>

auto total = std::ranges::fold_left(items, 0, std::plus{});
auto active = items | std::views::filter([](auto& i){ return i.active; });
std::ranges::sort(v);
```

**Use standard containers**: `std::vector` by default, `std::array` for fixed size, `std::unordered_map`/`std::map` as needed, `std::string`/`std::string_view` (non-owning views for read-only params). `std::span` for contiguous ranges without owning.

**Error handling:** exceptions for exceptional/unrecoverable errors; `std::optional<T>` for "maybe absent" and `std::expected<T, E>` (C++23) for recoverable failures with an error value:
```cpp
std::expected<int, std::string> parse(std::string_view s) {
    int out{};
    auto [_, ec] = std::from_chars(s.data(), s.data() + s.size(), out);
    if (ec != std::errc{}) return std::unexpected("not a number");
    return out;
}
```
Provide strong exception guarantees where feasible; never let exceptions escape destructors.

**Concurrency:** `std::thread`/`std::jthread` (auto-joining, C++20), `std::mutex` + `std::lock_guard`/`scoped_lock`, `std::atomic` for lock-free counters, `std::async`/`std::future` for task results. Protect all shared mutable state; run with ThreadSanitizer.

**Move semantics:** enable moves for expensive types; `std::move` to transfer, but don't move from something you still use. Return by value and rely on RVO - don't `std::move` a local return.

**Testing (GoogleTest):**
```cpp
#include <gtest/gtest.h>

TEST(Parse, ValidNumber) {
    auto r = parse("42");
    ASSERT_TRUE(r.has_value());
    EXPECT_EQ(*r, 42);
}

TEST(Parse, RejectsJunk) {
    EXPECT_FALSE(parse("x").has_value());
}
```
Build tests via CMake + CTest; add AddressSanitizer/UBSan builds to CI (`-fsanitize=address,undefined`).

**Tooling:** CMake (targets, not global flags), `clang-format` for style, `clang-tidy` with core-guidelines checks, compile with `-Wall -Wextra -Wpedantic` and treat warnings as errors. Prefer `auto` where the type is obvious, spell it out where clarity demands.

## Anti-patterns

- Raw `new`/`delete` and owning raw pointers - use smart pointers/RAII.
- C-style casts and arrays; use `static_cast`/`std::array`/`std::vector`.
- Manual index loops where `<algorithm>`/ranges express the intent.
- Returning raw pointers with unclear ownership.
- `#define` macros for constants/functions - use `constexpr`/`inline`.
- Passing large objects by value unintentionally (copies); pass by `const&`.
- Dangling `string_view`/`span` into a temporary that's already destroyed.
- Catching exceptions in destructors' path or letting them escape a destructor.
