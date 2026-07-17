# redis

> Source: curated best practices | 2026

---

# Redis - Caching, rate limiting, and data-structure patterns

Redis 7 is an in-memory data-structure store used for caching, rate limiting, distributed locks, queues, and ephemeral session state. Correct use hinges on always setting TTLs, choosing the right data structure, atomic operations (Lua scripts / transactions) to avoid races, and treating Redis as a fast-but-volatile layer in front of a durable store — never the source of truth for data you can't lose. This skill covers cache-aside, key design, atomic counters and rate limiting, locks, and eviction.

## When to Activate

Use when the task involves Redis:
- Caching database or API results (cache-aside, invalidation, TTLs)
- Rate limiting or throttling
- Distributed locks or coordination
- Counters, leaderboards, sessions, or queues
- Choosing a data structure or configuring eviction/memory

## Patterns and Best Practices

### Key naming and TTLs — always expire

```
# Structured, colon-namespaced, versioned keys
user:v1:42:profile
ratelimit:login:203.0.113.7
session:9f2c...

SET user:v1:42:profile "<json>" EX 3600        # NEVER SET without a TTL for cache data
```

Every cache key gets a TTL. Keys without expiry accumulate until eviction or OOM. Version the prefix (`v1`) so a schema change is a clean cutover, not a migration.

### Cache-aside (lazy loading)

```python
def get_profile(uid: int) -> dict:
    key = f"user:v1:{uid}:profile"
    cached = r.get(key)
    if cached is not None:
        return json.loads(cached)
    profile = db.fetch_profile(uid)                 # miss → load from source of truth
    # jitter the TTL to prevent thundering-herd expiry
    r.set(key, json.dumps(profile), ex=3600 + random.randint(0, 300))
    return profile

def update_profile(uid: int, data: dict) -> None:
    db.update_profile(uid, data)
    r.delete(f"user:v1:{uid}:profile")              # invalidate on write, don't stale-write
```

Invalidate (delete) on write rather than writing the new value into the cache — deletion is race-safe; write-through can lose a concurrent update.

### Atomic operations — avoid read-modify-write races

```python
# Atomic counter — INCR is a single atomic op, no GET/SET race
views = r.incr(f"post:v1:{pid}:views")

# Multi-step invariants: Lua runs atomically server-side
lua = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current + tonumber(ARGV[1]) > tonumber(ARGV[2]) then return 0 end
return redis.call('INCRBY', KEYS[1], ARGV[1])
"""
allowed = r.eval(lua, 1, quota_key, amount, limit)
```

Never `GET` then `SET` back a modified value across a network round trip under concurrency — use `INCR`/`INCRBY`, `SETNX`, or a Lua script.

### Rate limiting — fixed window with atomic INCR + EXPIRE

```python
def allow(ip: str, limit: int = 100, window: int = 60) -> bool:
    key = f"ratelimit:api:{ip}"
    n = r.incr(key)
    if n == 1:
        r.expire(key, window)      # set TTL only on first hit of the window
    return n <= limit
```

For smoother limiting use a sliding-window log (sorted set of timestamps, trim with `ZREMRANGEBYSCORE`) or token bucket in Lua.

### Distributed lock (safe release)

```python
token = uuid.uuid4().hex
if r.set(lock_key, token, nx=True, ex=10):     # acquire only if absent, auto-expire
    try:
        do_critical_section()
    finally:
        # release only if WE still hold it — compare-and-delete via Lua
        r.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end",
               1, lock_key, token)
```

The random token + Lua compare-and-delete prevents releasing a lock that already expired and was re-acquired by someone else. For strong guarantees across nodes, use Redlock or a real coordinator — a single-instance lock is best-effort.

### Data structures — pick the right one

| Need | Structure | Ops |
|------|-----------|-----|
| Counter / metric | String | `INCR`, `INCRBY` |
| Leaderboard / time-ordered | Sorted set | `ZADD`, `ZRANGE`, `ZREVRANK` |
| Queue / job list | List | `LPUSH` / `BRPOP` |
| Set membership / tags | Set | `SADD`, `SISMEMBER` |
| Object fields | Hash | `HSET`, `HGETALL` |
| Stream / event log | Stream | `XADD`, `XREADGROUP` |

### Memory and eviction

Set `maxmemory` and an eviction policy: `allkeys-lru` for a pure cache, `volatile-lru` when Redis also holds non-cache keys that must not be evicted. Monitor `used_memory`, hit rate (`keyspace_hits/misses`), and evicted keys.

## Anti-patterns

- `SET` without a TTL for cache data — unbounded memory growth.
- Read-modify-write across the network under concurrency — use `INCR`/Lua atomics.
- Treating Redis as the durable source of truth — it's volatile; back critical state with a real DB.
- `KEYS *` in production — O(n) blocks the server; use `SCAN`.
- Releasing a distributed lock with a blind `DEL` — you may delete someone else's lock.
- Storing large blobs / whole datasets in Redis instead of a hot subset.
- No `maxmemory`/eviction policy — an OOM crashes the instance.
- Synchronized TTLs causing thundering-herd expiry — add jitter.
