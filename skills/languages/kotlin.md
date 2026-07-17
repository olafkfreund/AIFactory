# kotlin

> Source: curated best practices | 2026

---

# Kotlin - Null-safe, concise JVM and multiplatform code

This skill equips the coder to write idiomatic Kotlin (2.0+, JVM target 21) that exploits null safety, data classes, sealed hierarchies with `when`, extension functions, and coroutines for structured concurrency. It assumes a Gradle (Kotlin DSL) project, ktlint/detekt for style, JUnit 5 + Kotest/MockK for tests, and immutability by default with `val` and read-only collections.

## When to Activate

Use when the task involves Kotlin:
- Writing or modifying `.kt` files, Gradle Kotlin modules
- Building Android apps, Spring Boot / Ktor services, or KMP libraries
- Anything referencing `build.gradle.kts`, coroutines, `data class`, `suspend`

## Idioms and Best Practices

**Null safety is the point.** Make nullability explicit and let the compiler enforce it:
```kotlin
fun find(id: String): User? = repo[id]

val name = find(id)?.name ?: "unknown"     // safe call + elvis
find(id)?.let { notify(it) }               // run only if non-null
```
Never use `!!` to force-unwrap unless a null is a genuine bug you want to crash on. Prefer `?.`, `?:`, `let`, and smart casts.

**Data classes for values** - free `equals`/`hashCode`/`copy`/`toString`:
```kotlin
data class Point(val x: Double, val y: Double)

val moved = point.copy(x = point.x + 1)    // non-destructive update
```

**Sealed classes/interfaces + exhaustive `when`** for domain modeling:
```kotlin
sealed interface Result<out T> {
    data class Ok<T>(val value: T) : Result<T>
    data class Err(val message: String) : Result<Nothing>
}

fun handle(r: Result<Int>): Int = when (r) {
    is Result.Ok -> r.value
    is Result.Err -> 0                     // when is exhaustive; no else needed
}
```

**`val` over `var`, immutable collections over mutable.** Use `listOf`/`mapOf` (read-only) by default; `mutableListOf` only where you actually mutate. Prefer expression bodies for small functions:
```kotlin
fun area(r: Double) = Math.PI * r * r
```

**Extension functions** to add behavior without inheritance - keep them focused and discoverable:
```kotlin
fun String.toSlug() = trim().lowercase().replace(Regex("\\s+"), "-")
```

**Collection operations** read like pipelines:
```kotlin
val counts = orders
    .filter { it.paid }
    .groupingBy { it.region }
    .eachCount()

val names = users.filter { it.active }.map { it.name }
```
Use `associateBy`, `sumOf`, `firstOrNull`, `mapNotNull`, `fold`.

**Coroutines for async / concurrency** with structured concurrency - launch within a scope so children are cancelled together:
```kotlin
suspend fun loadAll(ids: List<String>): List<User> = coroutineScope {
    ids.map { id -> async { fetch(id) } }.awaitAll()
}
```
Mark I/O functions `suspend`; switch dispatchers with `withContext(Dispatchers.IO)` for blocking calls. Never block a coroutine with `Thread.sleep` or `.get()`.

**Scope functions** used with intent: `let` (transform/null-guard), `apply` (configure and return receiver), `also` (side effect), `run`/`with` (compute on receiver). Don't nest them into puzzles.

**Error handling:** throw exceptions for exceptional cases; model expected failures with a sealed `Result` type or `kotlin.Result`. Use `requireNotNull`/`require`/`check` for preconditions:
```kotlin
fun charge(amount: Int) {
    require(amount > 0) { "amount must be positive" }
    ...
}
```

**Testing (JUnit 5 + Kotest assertions / MockK):**
```kotlin
class ParserTest {
    @Test
    fun `parses a valid number`() {
        assertEquals(42, Parser().parse("42"))
    }

    @Test
    fun `throws on junk`() {
        assertThrows<IllegalArgumentException> { Parser().parse("x") }
    }
}
```
Backtick test names read well. Use MockK for mocking, `runTest` for coroutine tests.

**Tooling:** Gradle Kotlin DSL, ktlint (format) and detekt (static analysis) in CI, `kotlinc` warnings treated seriously. Prefer the standard library's rich collection/string APIs.

## Anti-patterns

- `!!` non-null assertions to bypass null safety instead of handling null.
- `var` and mutable collections where `val`/read-only would do.
- `lateinit` overused to dodge nullability, then crashing on access.
- `GlobalScope.launch` - unstructured, leaks; use a bounded `CoroutineScope`.
- Blocking inside coroutines (`Thread.sleep`, blocking `.get()`).
- Nested scope-function chains (`apply { let { run { ... } } }`) nobody can read.
- Reproducing Java getter/setter/POJO boilerplate instead of `data class`.
- Catching broad `Exception` and ignoring it.
