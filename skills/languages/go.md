# go

> Source: curated best practices | 2026

---

# Go - Simple, concurrent, explicit-error backend code

This skill equips the coder to write idiomatic Go 1.22+ following the standard project conventions: small interfaces defined by consumers, explicit error handling with wrapping, goroutines coordinated by contexts and channels, and a heavy lean on the standard library. It assumes `go.mod` modules, `gofmt`/`goimports`, `go vet`, `staticcheck`, and table-driven tests with the `testing` package.

## When to Activate

Use when the task involves Go:
- Writing or modifying `.go` files, packages, or modules
- Building HTTP services, CLIs, workers, or libraries in Go
- Anything referencing `go.mod`, `go test`, `goroutine`, `channel`, `context`

## Idioms and Best Practices

**Project layout** (standard, not enforced but expected):
```
myapp/
  go.mod
  cmd/myapp/main.go      // entrypoint(s)
  internal/store/store.go // private packages
  http.go
  http_test.go
```
Put code you don't want imported externally under `internal/`.

**Errors are values - handle them explicitly.** Wrap with `%w` to preserve the chain:
```go
func load(path string) (Config, error) {
    b, err := os.ReadFile(path)
    if err != nil {
        return Config{}, fmt.Errorf("read config %s: %w", path, err)
    }
    ...
}
```
Inspect with `errors.Is` (sentinels) and `errors.As` (typed errors). Define sentinels as `var ErrNotFound = errors.New("not found")`. Never discard errors with `_` unless truly irrelevant.

**Accept interfaces, return structs.** Define interfaces where they're consumed, keep them small:
```go
type Store interface {
    Get(ctx context.Context, id string) (User, error)
}
```
A one-method interface is normal and good.

**Context first.** Any function doing I/O takes `ctx context.Context` as its first argument and respects cancellation. Never store a context in a struct.

**Concurrency:** goroutines are cheap but must be owned. Use `sync.WaitGroup` or `errgroup.Group` to wait; pass a `context` for cancellation; use channels to communicate, mutexes to protect state. Always know how a goroutine exits.
```go
g, ctx := errgroup.WithContext(ctx)
for _, u := range urls {
    u := u
    g.Go(func() error { return fetch(ctx, u) })
}
if err := g.Wait(); err != nil { return err }
```
Guard shared maps/slices with `sync.Mutex`; run tests with `-race`.

**Defer for cleanup**, checking close errors where they matter:
```go
f, err := os.Open(path)
if err != nil { return err }
defer f.Close()
```

**Zero values are useful** - design types so the zero value works (`sync.Mutex`, `bytes.Buffer`). Prefer `nil` slices/maps handling over guarding for empty.

**Table-driven tests:**
```go
func TestParse(t *testing.T) {
    tests := []struct {
        name string
        in   string
        want int
        wantErr bool
    }{
        {"ok", "42", 42, false},
        {"bad", "x", 0, true},
    }
    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            got, err := Parse(tt.in)
            if (err != nil) != tt.wantErr {
                t.Fatalf("err = %v, wantErr %v", err, tt.wantErr)
            }
            if got != tt.want {
                t.Errorf("got %d, want %d", got, tt.want)
            }
        })
    }
}
```
Use `t.Helper()` in test helpers, `t.TempDir()` for filesystem, `httptest` for HTTP handlers. Run `go test -race ./...`.

**Standard library first:** `net/http` (with `http.ServeMux` routing patterns added in 1.22: `mux.HandleFunc("GET /users/{id}", ...)`), `encoding/json`, `log/slog` for structured logging, `slices` and `maps` generic helpers, `context`, `database/sql`.

**Generics** when they remove real duplication (`slices.SortFunc`, container types) - not by default.

**Formatting/lint:** `gofmt`/`goimports` (always), `go vet`, `staticcheck`. There is one true format; do not argue with it.

## Anti-patterns

- Ignoring errors (`v, _ := f()`) outside genuinely safe cases.
- Panicking for ordinary error conditions - reserve `panic` for unrecoverable programmer bugs.
- Starting goroutines with no clear termination or cancellation path (leaks).
- Storing `context.Context` in a struct field instead of passing it.
- Large, speculative interfaces defined next to the implementation.
- `interface{}`/`any` where a concrete type or generic works.
- Naked returns in long functions (obscure what's returned).
- Reinventing `sync`, `slices`, `maps`, or `errgroup` by hand.
