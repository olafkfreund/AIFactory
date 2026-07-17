# java

> Source: curated best practices | 2026

---

# Java - Modern, records-and-streams application code

This skill equips the coder to write modern Java 21 (LTS) that uses records for data, sealed interfaces + pattern matching for domain modeling, streams for transforms, and `Optional` instead of returning null. It assumes a Maven or Gradle project, immutable-by-default design, constructor injection over field injection, JUnit 5 + AssertJ for tests, and virtual threads for scalable I/O.

## When to Activate

Use when the task involves Java:
- Writing or modifying `.java` files, Maven/Gradle modules
- Building Spring Boot / Jakarta services, libraries, or CLIs in Java
- Anything referencing `pom.xml`, `build.gradle`, JUnit, `stream()`, records

## Idioms and Best Practices

**Project layout** (Maven standard):
```
src/main/java/com/acme/app/App.java
src/main/java/com/acme/app/UserService.java
src/test/java/com/acme/app/UserServiceTest.java
pom.xml
```

**Records for immutable data** - no boilerplate getters/equals/hashCode:
```java
public record Point(double x, double y) {
    public Point {                       // compact canonical constructor for validation
        if (Double.isNaN(x)) throw new IllegalArgumentException("x is NaN");
    }
}
```

**Sealed types + pattern matching** for closed hierarchies:
```java
sealed interface Shape permits Circle, Square {}
record Circle(double r) implements Shape {}
record Square(double side) implements Shape {}

double area(Shape s) {
    return switch (s) {
        case Circle c -> Math.PI * c.r() * c.r();
        case Square sq -> sq.side() * sq.side();
    };                                   // exhaustive; no default needed
}
```

**`Optional` for maybe-absent returns**, never return null from a public method:
```java
public Optional<User> findById(String id) { ... }

findById(id)
    .map(User::name)
    .orElse("unknown");
```
Do not use `Optional` for fields or method parameters - only return types.

**Streams for transforms**, but keep them readable; drop to a loop if a stream gets convoluted:
```java
Map<String, Long> counts = orders.stream()
    .filter(Order::isPaid)
    .collect(Collectors.groupingBy(Order::region, Collectors.counting()));
```

**Immutability and final.** Prefer `final` fields, immutable collections (`List.of`, `Map.of`, `Collectors.toUnmodifiableList`). Construct fully-initialized objects; avoid setters.

**Dependency injection by constructor**, not field `@Autowired`:
```java
@Service
public class UserService {
    private final UserRepository repo;
    public UserService(UserRepository repo) { this.repo = repo; }
}
```
This keeps the class testable and its dependencies explicit.

**Exceptions:** throw specific unchecked exceptions for programming errors; use checked exceptions sparingly and only when the caller can act. Always chain the cause: `throw new AppException("load failed", e)`. Never swallow in an empty `catch`. Use try-with-resources for anything `AutoCloseable`:
```java
try (var in = Files.newInputStream(path)) { ... }
```

**Concurrency:** prefer `java.util.concurrent` (`ExecutorService`, `ConcurrentHashMap`, `CompletableFuture`) over raw threads and `synchronized` where possible. On Java 21 use virtual threads for high-concurrency blocking I/O:
```java
try (var exec = Executors.newVirtualThreadPerTaskExecutor()) {
    exec.submit(task);
}
```

**Testing (JUnit 5 + AssertJ):**
```java
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.*;

class UserServiceTest {
    @Test
    void findsExistingUser() {
        var svc = new UserService(new InMemoryRepo());
        assertThat(svc.findById("1")).isPresent();
    }

    @Test
    void rejectsBlankId() {
        assertThatThrownBy(() -> svc.findById(""))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
```
Use `@ParameterizedTest` for table cases, Mockito for mocking collaborators.

**Tooling:** build with Maven/Gradle, format with `google-java-format` or Spotless, static analysis with SpotBugs/Error Prone, style with Checkstyle. Use `var` for obvious local types, spell out types on public APIs.

## Anti-patterns

- Returning `null` where `Optional` or an empty collection belongs.
- Mutable public fields and setter-driven half-constructed objects.
- Field injection (`@Autowired` on fields) - breaks testability.
- Catching `Exception`/`Throwable` broadly and swallowing it.
- Manual getter/equals/hashCode boilerplate where a `record` fits.
- Overusing inheritance where composition or sealed interfaces are clearer.
- Raw types (`List` instead of `List<String>`).
- Starting raw `Thread` objects instead of an executor / virtual threads.
