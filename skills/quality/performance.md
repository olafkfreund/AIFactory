# performance

> Source: curated best practices | 2026

---

# Performance - measure first, fix the real bottleneck, stop there

Most performance work is wasted because it is guesswork — someone optimizes a function that runs once while the real cost is a query in a loop firing ten thousand times. The discipline is the opposite of intuition: profile before you touch anything, find where the time and memory actually go, fix the one thing that dominates, measure again, and stop when it is fast enough. The biggest wins are almost never micro-optimizations; they are a better algorithm, a removed N+1 query, or a cache on a hot path. Premature optimization trades real readability for imagined speed — do not pay that until a measurement tells you to.

## When to Activate

Use when something is measurably slow, or a change is on a hot path:
- a request, job, page, or query is too slow and you need to know why
- code that runs per-row, per-request, or in a tight loop
- database access, especially inside loops (N+1)
- deciding whether to add a cache, an index, or a more complex algorithm
- reviewing a change that could blow up in time or memory at scale

## Principles and Practices

**Measure first — always.** Never optimize on a hunch. Profile with real tools and representative data: a CPU/wall-clock profiler (`py-spy`, `pprof`, Chrome DevTools, `perf`), query logs / `EXPLAIN ANALYZE`, and timing around suspect sections. The bottleneck is usually somewhere you did not expect. A change that "should be faster" but is not measured is just risk. Establish a baseline number, make the change, compare — no guessing whether it helped.

**Fix the dominant cost.** Amdahl's law: speeding up something that is 2% of runtime by 10x saves 1.8%. Find the part that dominates the profile and fix that. One well-chosen fix usually beats a dozen scattered tweaks. Once the top item is handled, re-profile — the bottleneck moves.

**N+1 queries are the classic killer.** Fetching a list and then querying per item turns one call into thousands.

```python
# WRONG: 1 query for orders + N queries, one per order, for its user
orders = db.query("SELECT * FROM orders")
for o in orders:
    o.user = db.query("SELECT * FROM users WHERE id = ?", o.user_id)  # N queries

# RIGHT: 2 queries total — batch the second, or JOIN
orders = db.query("SELECT * FROM orders")
users = db.query("SELECT * FROM users WHERE id IN (...)")  # one batched query
```

Use eager loading / joins / `IN` batching. This single pattern is behind a huge share of "the page is slow" reports. Watch for it in ORMs where the query is hidden behind attribute access.

**Algorithmic complexity beats constant factors.** An O(n²) nested loop over a growing list will eventually dominate no matter how tight the inner code is. Turning a repeated `list.contains` (O(n)) inside a loop into a `set`/`dict` lookup (O(1)) can take an operation from minutes to milliseconds.

```python
# WRONG O(n*m): "in a list" is a linear scan each time
dups = [x for x in a if x in b]          # b is a list → O(n*m)
# RIGHT O(n+m):
bset = set(b)
dups = [x for x in a if x in bset]       # set lookup → O(1) each
```

Know the complexity of the data structure you reach for. Sorting once (O(n log n)) to enable a linear pass often beats repeated scans.

**Cache the expensive and repeated — carefully.** A cache turns a repeated expensive computation or fetch into a lookup. Reach for the built-in first (`@lru_cache`, `functools.cache`, an HTTP cache header, a DB query cache) before building a cache class. But caching adds a hard problem: invalidation. Only cache data that is expensive to produce and tolerant of staleness, set a TTL, and know how it gets invalidated. A wrong cache serves stale data silently — worse than being slow.

**Do not optimize prematurely.** Clear, correct code first. Do not contort logic, add caches, or hand-roll a clever data structure for a path that runs rarely or over tiny data — you pay real readability cost for speed nobody will notice. "Fast enough" is a real target; past it, stop. The exception: do not knowingly choose a quadratic algorithm when a linear one is the same effort — that is not premature optimization, that is not writing a bug.

**Batch and stream at boundaries.** Prefer one bulk operation over many round-trips (bulk insert, batched API call). For large datasets, stream/paginate instead of loading everything into memory — an unbounded `SELECT *` or reading a whole file into RAM is a memory bomb at scale. Set limits.

**Indexes for the queries you actually run.** A missing index on a filtered/joined/sorted column turns a lookup into a full table scan. Read the query plan; add indexes to match real access patterns. But every index costs write speed and space — index for measured queries, not hypothetically.

**Watch memory, not just CPU.** Loading large collections, unbounded caches, and per-request allocations that scale with input can exhaust memory and trigger GC thrash or OOM kills. Bound anything that grows with load.

## Anti-patterns

- Optimizing without a profiler — guessing at the bottleneck and usually missing it.
- Micro-optimizing a cold path while an N+1 query or O(n²) loop dominates.
- Querying inside a loop instead of batching / joining.
- Linear scans (`x in list`) inside a loop where a set/dict would be O(1).
- Adding a cache with no TTL and no invalidation plan — silent stale data.
- Building a bespoke cache/pool/data-structure before measuring, for cold code.
- `SELECT *` with no limit, or loading an entire large file/collection into memory.
- Missing indexes on filtered/joined columns — or indexing everything and killing writes.
- Declaring victory without re-measuring against the baseline.
