# mongodb

> Source: curated best practices | 2026

---

# MongoDB - Document modeling, indexing, and safe aggregation

MongoDB 7 is a document database where schema design is driven by access patterns, not normalization. The core decisions are embed-versus-reference, index design that supports your equality/sort/range queries, and safe query construction that never lets user input become operators. This skill covers schema modeling, compound indexes following the ESR rule, aggregation pipelines, transactions, and injection-safe queries.

## When to Activate

Use when the task involves MongoDB:
- Modeling documents / deciding embed vs. reference
- Writing find/update queries or aggregation pipelines
- Adding indexes or diagnosing slow queries with explain()
- Multi-document transactions
- Preventing NoSQL operator injection

## Patterns and Best Practices

### Model for access patterns — embed vs reference

```javascript
// EMBED when data is read together and bounded (one-to-few)
{
  _id: ObjectId(),
  email: "a@b.com",
  addresses: [                       // read with the user, small, bounded
    { label: "home", city: "Oslo", postcode: "0123" }
  ]
}

// REFERENCE when unbounded, large, or shared (one-to-many / many-to-many)
// order document references user, does not embed it
{ _id: ObjectId(), userId: ObjectId("..."), totalCents: 4999, placedAt: ISODate() }
```

Rule of thumb: embed one-to-few and data-that-changes-together; reference one-to-many/unbounded (comments, orders, events) to avoid the 16 MB document limit and rewriting huge documents on every append.

### Indexing — the ESR rule (Equality, Sort, Range)

```javascript
// Query: find({ userId: X, status: "open" }).sort({ placedAt: -1 })
//        with an occasional range on placedAt
db.orders.createIndex(
  { userId: 1, status: 1, placedAt: -1 }   // Equality (userId,status) → Sort (placedAt)
);

// Partial index — only index the rows you query
db.orders.createIndex(
  { placedAt: -1 },
  { partialFilterExpression: { status: "open" } }
);

// Unique constraint
db.users.createIndex({ email: 1 }, { unique: true });
```

Always confirm with `db.orders.find(...).explain("executionStats")` — `COLLSCAN` in `winningPlan` on a hot path means a missing/unusable index; watch `totalDocsExamined` ≫ `nReturned`.

### Injection-safe queries — never pass raw user objects

```javascript
// DANGER: if req.body.username is { "$ne": null }, this returns any user
db.users.findOne({ username: req.body.username, password: req.body.password });

// SAFE: coerce to expected primitive type, reject objects
const username = String(req.body.username);
const password = String(req.body.password);
db.users.findOne({ username });   // then verify hashed password in app code
```

The NoSQL-injection vector is user input that becomes an operator (`$ne`, `$gt`, `$where`, `$regex`). Cast to `String`/`Number` at the boundary, validate with a schema (e.g. Zod/JSON Schema), and never build `$where` from input. Enforce shape server-side with schema validation:

```javascript
db.createCollection("users", { validator: { $jsonSchema: {
  bsonType: "object", required: ["email"],
  properties: { email: { bsonType: "string", pattern: "^.+@.+$" } }
}}});
```

### Aggregation pipeline

```javascript
db.orders.aggregate([
  { $match: { placedAt: { $gte: since } } },        // filter FIRST (uses index)
  { $group: { _id: "$userId", total: { $sum: "$totalCents" }, n: { $sum: 1 } } },
  { $sort:  { total: -1 } },
  { $limit: 20 }
]);
```

Put `$match` and `$sort` as early as possible so they use indexes before documents flow downstream. Use `$project` to drop large fields early.

### Transactions (replica set / sharded)

```javascript
const session = client.startSession();
try {
  await session.withTransaction(async () => {
    await accounts.updateOne({ _id: src }, { $inc: { balanceCents: -amt } }, { session });
    await accounts.updateOne({ _id: dst }, { $inc: { balanceCents:  amt } }, { session });
  });
} finally { await session.endSession(); }
```

Prefer single-document atomic updates (`$inc`, `$set`, `$push`) — they're atomic without a transaction. Reach for multi-document transactions only when you truly cross documents.

### Writes and durability

Use `writeConcern: { w: "majority" }` for data that must survive a primary failover. Use bulk operations (`bulkWrite`) for batch inserts/updates instead of a network round trip per document.

## Anti-patterns

- Passing `req.body` objects straight into a query — operator injection; cast to primitives.
- Building `$where` or `$regex` from user input.
- Embedding unbounded arrays (comments, events) — hits the 16 MB doc limit and rewrites the whole doc on append.
- Treating MongoDB like a relational DB: heavy `$lookup` joins everywhere instead of modeling for reads.
- Creating an index per field instead of ESR-ordered compound indexes.
- Default write concern for critical data — set `w: "majority"`.
- Unbounded `find()` without a `.limit()` on large collections.
- Skipping schema validation and relying only on app code for shape.
