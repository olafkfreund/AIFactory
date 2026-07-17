# dotnet

> Source: curated best practices | 2026

---

# .NET - ASP.NET Core Web APIs with C# 12

This skill equips the coder to build production ASP.NET Core 8 (LTS) Web APIs in C# 12, using minimal APIs or controllers, Entity Framework Core 8, DTO records, dependency injection, `IOptions<T>` config binding, JWT bearer auth, and `ProblemDetails`-based error handling. It enforces async-all-the-way, EF migrations, `WebApplicationFactory` integration tests with `xUnit`, and nullable reference types enabled. Assumes a layered solution (API / Application / Infrastructure) or a focused single project for smaller services.

## When to Activate

Use when building with .NET / ASP.NET Core:
- Building C# Web APIs, minimal APIs, or microservices
- Files with `WebApplication.CreateBuilder`, `[ApiController]`, `DbContext`, or `Program.cs`
- Adding endpoints, EF Core models/migrations, DI registration, or JWT auth
- Config binding with `IOptions<T>` or `ProblemDetails` error responses

## Patterns and Best Practices

Program.cs — composition root (minimal hosting model):

```csharp
// Program.cs
var builder = WebApplication.CreateBuilder(args);

builder.Services.AddDbContext<AppDbContext>(o =>
    o.UseNpgsql(builder.Configuration.GetConnectionString("Default")));
builder.Services.AddScoped<IUserService, UserService>();
builder.Services.Configure<JwtOptions>(builder.Configuration.GetSection("Jwt"));
builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer();
builder.Services.AddAuthorization();
builder.Services.AddProblemDetails();
builder.Services.AddControllers();

var app = builder.Build();
app.UseExceptionHandler();   // maps to ProblemDetails
app.UseAuthentication();
app.UseAuthorization();
app.MapControllers();
app.Run();

public partial class Program { } // exposed for WebApplicationFactory
```

DTOs as records, separate from EF entities:

```csharp
public record CreateUserRequest(string Email, string Password);
public record UserResponse(int Id, string Email);
```

EF Core entity + DbContext:

```csharp
// Infrastructure/User.cs
public class User
{
    public int Id { get; set; }
    public required string Email { get; set; }
    public required string HashedPassword { get; set; }
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}

// Infrastructure/AppDbContext.cs
public class AppDbContext(DbContextOptions<AppDbContext> options) : DbContext(options)
{
    public DbSet<User> Users => Set<User>();

    protected override void OnModelCreating(ModelBuilder b)
    {
        b.Entity<User>().HasIndex(u => u.Email).IsUnique();
    }
}
```

Service layer with async EF access:

```csharp
// Application/UserService.cs
public interface IUserService
{
    Task<UserResponse> CreateAsync(CreateUserRequest req, CancellationToken ct);
}

public class UserService(AppDbContext db, IPasswordHasher hasher) : IUserService
{
    public async Task<UserResponse> CreateAsync(CreateUserRequest req, CancellationToken ct)
    {
        if (await db.Users.AnyAsync(u => u.Email == req.Email, ct))
            throw new ConflictException("email already registered");

        var user = new User { Email = req.Email, HashedPassword = hasher.Hash(req.Password) };
        db.Users.Add(user);
        await db.SaveChangesAsync(ct);
        return new UserResponse(user.Id, user.Email);
    }
}
```

Controller — thin, validated, async, cancellation-aware:

```csharp
// Api/UsersController.cs
[ApiController]
[Route("users")]
public class UsersController(IUserService service) : ControllerBase
{
    [HttpPost]
    [ProducesResponseType(StatusCodes.Status201Created)]
    public async Task<ActionResult<UserResponse>> Create(
        CreateUserRequest req, CancellationToken ct)
    {
        var user = await service.CreateAsync(req, ct);
        return CreatedAtAction(nameof(Get), new { id = user.Id }, user);
    }

    [HttpGet("{id:int}")]
    [Authorize]
    public async Task<ActionResult<UserResponse>> Get(int id, CancellationToken ct)
        => await service.GetAsync(id, ct) is { } u ? Ok(u) : NotFound();
}
```

Centralized error handling via `IExceptionHandler`:

```csharp
// Api/GlobalExceptionHandler.cs
public class GlobalExceptionHandler : IExceptionHandler
{
    public async ValueTask<bool> TryHandleAsync(
        HttpContext ctx, Exception ex, CancellationToken ct)
    {
        var (status, title) = ex switch
        {
            ConflictException => (StatusCodes.Status409Conflict, ex.Message),
            NotFoundException => (StatusCodes.Status404NotFound, ex.Message),
            _ => (StatusCodes.Status500InternalServerError, "internal error"),
        };
        await Results.Problem(title: title, statusCode: status).ExecuteAsync(ctx);
        return true;
    }
}
// register: builder.Services.AddExceptionHandler<GlobalExceptionHandler>();
```

Config binding, not magic strings:

```csharp
public class JwtOptions { public string Issuer { get; set; } = ""; public int ExpiryMinutes { get; set; } }
// inject IOptions<JwtOptions>; secrets come from env / user-secrets / key vault
```

Integration test with WebApplicationFactory:

```csharp
// Tests/UsersTests.cs
public class UsersTests(WebApplicationFactory<Program> factory)
    : IClassFixture<WebApplicationFactory<Program>>
{
    [Fact]
    public async Task Create_Returns201()
    {
        var client = factory.CreateClient();
        var resp = await client.PostAsJsonAsync("/users",
            new CreateUserRequest("a@b.com", "secret123"));
        Assert.Equal(HttpStatusCode.Created, resp.StatusCode);
    }
}
```

Migrations: `dotnet ef migrations add Init && dotnet ef database update`.

## Anti-patterns

- Blocking on async with `.Result` / `.Wait()` — causes thread-pool starvation and deadlocks; await instead.
- Returning EF entities from controllers instead of DTOs — leaks schema and lazy-loads.
- Not flowing `CancellationToken` into EF/HTTP calls.
- `new`-ing dependencies inside classes instead of constructor injection via DI.
- `EnsureCreated()` in production instead of EF migrations.
- Swallowing exceptions per-action rather than a global `IExceptionHandler` + `ProblemDetails`.
- Nullable reference types disabled, or hardcoded connection strings/secrets in `appsettings.json`.
