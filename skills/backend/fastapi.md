# fastapi

> Source: curated best practices | 2026

---

# FastAPI - Async Python APIs with Pydantic v2

This skill equips the coder to build production FastAPI services on Python 3.11+, FastAPI 0.11x+, and Pydantic v2. It enforces typed request/response models, dependency-injected DB sessions and auth, structured error handling via exception handlers, async-first I/O, and `TestClient`/`httpx.AsyncClient` tests. It assumes a `src/` layout with routers split by domain, settings loaded from the environment via `pydantic-settings`, and SQLAlchemy 2.0 (async) or SQLModel for persistence.

## When to Activate

Use when building with FastAPI:
- Creating REST/JSON APIs or async microservices in Python
- Files importing `fastapi`, `APIRouter`, `pydantic.BaseModel`, or `pydantic-settings`
- Adding endpoints, dependency injection, OAuth2/JWT auth, or OpenAPI docs
- Async SQLAlchemy 2.0 / SQLModel data access behind an HTTP API

## Patterns and Best Practices

Project structure:

```
src/app/
  main.py            # app factory, router include, exception handlers, lifespan
  config.py          # Settings (pydantic-settings)
  db.py              # engine, async session dependency
  models.py          # SQLAlchemy models
  schemas.py         # Pydantic v2 request/response models
  deps.py            # shared dependencies (get_db, get_current_user)
  routers/
    users.py
tests/
  test_users.py
```

Config from environment (never hardcode secrets):

```python
# config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    jwt_secret: str
    jwt_expire_minutes: int = 30

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
```

Pydantic v2 schemas — separate input from output, never return ORM objects raw:

```python
# schemas.py
from pydantic import BaseModel, ConfigDict, EmailStr, Field

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # ORM -> schema
    id: int
    email: EmailStr
```

Async DB session as a dependency (SQLAlchemy 2.0):

```python
# db.py
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from .config import get_settings

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
```

Router with typed responses and dependency injection:

```python
# routers/users.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..deps import get_db, get_current_user
from ..models import User
from ..schemas import UserCreate, UserOut

router = APIRouter(prefix="/users", tags=["users"])

@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)) -> User:
    existing = await db.scalar(select(User).where(User.email == payload.email))
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    user = User(email=payload.email, hashed_password=hash_password(payload.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user

@router.get("/me", response_model=UserOut)
async def me(current: User = Depends(get_current_user)) -> User:
    return current
```

OAuth2 password + JWT auth dependency:

```python
# deps.py
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from .config import get_settings

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/token")

async def get_current_user(token: str = Depends(oauth2), db=Depends(get_db)) -> User:
    cred_exc = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token",
                             headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise cred_exc
    user = await db.get(User, user_id)
    if user is None:
        raise cred_exc
    return user
```

App factory, lifespan, and centralized error handling:

```python
# main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .routers import users

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield  # startup/shutdown hooks (warm caches, close pools)

def create_app() -> FastAPI:
    app = FastAPI(title="Service", lifespan=lifespan)
    app.include_router(users.router)

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app

app = create_app()
```

Testing with a real ASGI transport:

```python
# tests/test_users.py
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app

@pytest.mark.asyncio
async def test_create_user():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/users", json={"email": "a@b.com", "password": "secret123"})
    assert resp.status_code == 201
    assert resp.json()["email"] == "a@b.com"
```

Override dependencies in tests instead of monkeypatching internals:

```python
app.dependency_overrides[get_db] = lambda: fake_session
```

## Anti-patterns

- Returning SQLAlchemy models directly without `response_model` / `from_attributes` — leaks columns and skips validation.
- Blocking I/O (`requests`, `time.sleep`, sync DB drivers) inside `async def` handlers — use `httpx`, `asyncio.sleep`, async drivers, or `run_in_threadpool`.
- Instantiating `Settings()` or DB engines at import time in many modules instead of a cached provider.
- Catching every exception and returning 200 — let exception handlers map errors to proper status codes.
- Putting business logic in route functions; keep routes thin and push logic into service functions.
- Using Pydantic v1 patterns (`class Config`, `.dict()`, `orm_mode`) — use `model_config`, `.model_dump()`, `from_attributes`.
