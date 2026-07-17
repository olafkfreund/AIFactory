# sqlalchemy

> Source: curated best practices | 2026

---

# SQLAlchemy - ORM models, sessions, and efficient querying

SQLAlchemy 2.0 is the Python ORM and Core toolkit. The 2.0 API is typed (`Mapped[...]`, `mapped_column`), uses `select()` everywhere, and makes the unit-of-work session lifecycle explicit. Correct use means scoping sessions to a request/task, avoiding N+1 queries with eager loading, using server-side parameters (never string interpolation), and managing transactions and migrations with Alembic. This skill covers declarative models, session management, relationship loading strategies, and safe query construction.

## When to Activate

Use when the task involves SQLAlchemy:
- Defining ORM models / relationships (`Mapped`, `relationship`)
- Writing queries with `select()`, joins, or aggregates
- Session/transaction lifecycle or connection pooling
- Diagnosing N+1 query problems / choosing loading strategies
- Alembic migrations

## Patterns and Best Practices

### Declarative models (2.0 typed style)

```python
from datetime import datetime
from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase): ...

class User(Base):
    __tablename__ = "users"
    id:    Mapped[int]  = mapped_column(primary_key=True)
    email: Mapped[str]  = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    orders: Mapped[list["Order"]] = relationship(back_populates="user")

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    total_cents: Mapped[int] = mapped_column()
    user: Mapped["User"] = relationship(back_populates="orders")
```

Index foreign keys, add explicit `ondelete`, and keep `back_populates` symmetric so both sides stay in sync.

### Engine and session — one engine, scoped sessions

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine(
    DATABASE_URL,
    pool_size=10, max_overflow=5, pool_pre_ping=True,   # pre_ping drops dead connections
    pool_recycle=1800,
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

# One session per request/unit of work — never one global session
def get_db():
    with SessionLocal() as session:      # closes/returns connection to pool
        yield session
```

Create the engine once at process start. A `Session` is not thread-safe — scope it to a single request or task, never share it across threads/async tasks.

### Querying with select() and safe parameters

```python
from sqlalchemy import select

# Parameters are bound by the driver — no injection possible
stmt = select(User).where(User.email == email_input).limit(1)
user = session.scalars(stmt).first()

# Raw SQL when needed: use text() + bound params, never f-strings
from sqlalchemy import text
rows = session.execute(
    text("SELECT id FROM users WHERE status = :s"), {"s": "active"}
).all()
```

Even in raw SQL, pass values as bound params (`:name`) — never build the string with user input.

### Kill N+1 with eager loading

```python
from sqlalchemy.orm import selectinload, joinedload

# BAD: lazy loads orders per user → 1 + N queries
users = session.scalars(select(User)).all()
for u in users:
    print(u.orders)          # a query EACH iteration

# GOOD: one extra query for all orders (collections → selectinload)
users = session.scalars(select(User).options(selectinload(User.orders))).all()

# For many-to-one / single related row, joinedload is one JOINed query
order = session.scalars(
    select(Order).where(Order.id == oid).options(joinedload(Order.user))
).first()
```

`selectinload` for collections (one-to-many), `joinedload` for scalar relations (many-to-one). Set `lazy="raise"` on relationships in hot code to make accidental lazy loads fail loudly.

### Transactions

```python
with SessionLocal() as session:
    with session.begin():                 # BEGIN … COMMIT, ROLLBACK on exception
        session.add(Order(user_id=uid, total_cents=4999))
        session.execute(update(Account).where(...).values(balance=Account.balance - 4999))
    # committed here
```

Use `session.begin()` as a context manager for atomic units. Don't call `commit()` scattered mid-function; group writes into one transaction.

### Migrations (Alembic)

Use autogenerate as a starting point but review every migration — Alembic misses server defaults, index renames, and type changes. Run migrations as a deploy step, not from app startup, and make them reversible (`upgrade`/`downgrade`).

## Anti-patterns

- A single global `Session` shared across threads/requests — not thread-safe; leaks state.
- Lazy-loading relationships in loops → N+1 queries; use `selectinload`/`joinedload`.
- f-string / `.format()` SQL — even with `text()`, use bound `:params`.
- Creating a new `Engine` per request — recreate the pool = connection storms.
- Calling `.all()` on huge tables — paginate or stream with `yield_per`.
- Leaving `expire_on_commit=True` then accessing attributes after commit (extra queries) — set `False` when you return detached objects.
- Trusting Alembic autogenerate blindly without reviewing the diff.
- Missing `pool_pre_ping` → stale connections after DB restart/failover raise errors.
