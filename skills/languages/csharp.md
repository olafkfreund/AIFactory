# csharp

> Source: curated best practices | 2026

---

# C# - Modern .NET, nullable-aware, async-first code

This skill equips the coder to write modern C# 12 on .NET 8+ with nullable reference types enabled, records for immutable data, pattern matching, LINQ for transforms, and async/await for all I/O. It assumes an SDK-style project, `dotnet format`, analyzers as errors, dependency injection via the built-in container, and xUnit + FluentAssertions for tests.

## When to Activate

Use when the task involves C#:
- Writing or modifying `.cs` files, `.csproj` projects, or solutions
- Building ASP.NET Core APIs, worker services, or libraries in C#
- Anything referencing `dotnet`, `.csproj`, xUnit, LINQ, `async`/`await`

## Idioms and Best Practices

**Enable nullable and treat warnings as errors** in the `.csproj`:
```xml
<PropertyGroup>
  <TargetFramework>net8.0</TargetFramework>
  <Nullable>enable</Nullable>
  <ImplicitUsings>enable</ImplicitUsings>
  <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
</PropertyGroup>
```
The compiler now tracks nullability; annotate reference types honestly (`string?` vs `string`).

**Records for immutable data / DTOs:**
```csharp
public record Point(double X, double Y);
public record User(string Id, string Name)
{
    public bool IsActive { get; init; }   // init-only setters keep immutability
}
```
Use `with` expressions for non-destructive updates: `user with { Name = "new" }`.

**Pattern matching and switch expressions:**
```csharp
decimal Area(Shape s) => s switch
{
    Circle c => Math.PI * c.R * c.R,
    Square sq => sq.Side * sq.Side,
    _ => throw new ArgumentOutOfRangeException(nameof(s))
};
```
Property, tuple, and relational patterns (`is > 0`) reduce nested ifs.

**LINQ for transforms**, method syntax preferred:
```csharp
var counts = orders
    .Where(o => o.IsPaid)
    .GroupBy(o => o.Region)
    .ToDictionary(g => g.Key, g => g.Count());
```
Materialize with `ToList()`/`ToArray()` when you'll enumerate more than once; keep it lazy otherwise. Beware deferred execution over disposed contexts (EF Core).

**Async all the way** - never block on async with `.Result`/`.Wait()` (deadlocks, thread starvation):
```csharp
public async Task<User?> GetUserAsync(string id, CancellationToken ct)
{
    return await _repo.FindAsync(id, ct);
}
```
Flow `CancellationToken` through every async call. Use `await using` for `IAsyncDisposable`. Return `Task`/`ValueTask`, not `void` (except event handlers).

**Dependency injection** via constructor and the built-in container:
```csharp
builder.Services.AddScoped<IUserService, UserService>();

public class UserService(IUserRepository repo) : IUserService  // primary constructor
{
    public Task<User?> GetAsync(string id) => repo.FindAsync(id);
}
```

**Resource management:** `using` declarations for `IDisposable`:
```csharp
using var stream = File.OpenRead(path);
```

**Error handling:** throw specific exceptions; use guard clauses at method entry (`ArgumentNullException.ThrowIfNull(x)`). Don't catch what you can't handle; always preserve stack (`throw;` not `throw ex;`).

**Testing (xUnit + FluentAssertions):**
```csharp
public class UserServiceTests
{
    [Fact]
    public async Task GetAsync_ReturnsUser_WhenExists()
    {
        var svc = new UserService(new InMemoryRepo());
        var user = await svc.GetAsync("1");
        user.Should().NotBeNull();
    }

    [Theory]
    [InlineData("")]
    [InlineData(null)]
    public async Task GetAsync_Throws_OnBlankId(string? id)
    {
        var act = () => svc.GetAsync(id!);
        await act.Should().ThrowAsync<ArgumentException>();
    }
}
```
Use Moq/NSubstitute for mocks, `WebApplicationFactory` for ASP.NET integration tests.

**Tooling:** `dotnet build`, `dotnet test`, `dotnet format`. Enable Roslyn analyzers and .editorconfig. Use `var` when the type is obvious, explicit types on public members. File-scoped namespaces (`namespace Acme.App;`).

## Anti-patterns

- Blocking on async (`.Result`, `.Wait()`, `.GetAwaiter().GetResult()`) - deadlock risk.
- `async void` outside event handlers - unobservable exceptions.
- Ignoring nullable warnings or sprinkling `!` to silence them.
- `catch (Exception) {}` swallowing; `throw ex;` losing the stack trace.
- Mutable public fields and DTOs where records/init properties fit.
- Not passing `CancellationToken` through async chains.
- Service Locator (`GetService` everywhere) instead of constructor injection.
- Overusing `dynamic` and reflection where generics or patterns work.
