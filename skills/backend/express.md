# express

> Source: curated best practices | 2026

---

# Express - Minimal Node.js HTTP APIs (TypeScript)

This skill equips the coder to build production Express 4.x/5.x APIs on Node 20+ with TypeScript. It enforces a layered structure (routes -> controllers -> services -> repositories), schema validation with Zod, centralized async error handling, auth middleware with JWT, `helmet`/`cors`/rate limiting, config from `process.env`, and integration tests with `supertest` + Jest/Vitest. It assumes ESM, `express.json()` body parsing, and that the app is exported separately from the server for testability.

## When to Activate

Use when building with Express:
- Building Node.js REST APIs or middleware-based HTTP servers
- Files importing `express`, `Router`, `zod`, `jsonwebtoken`, or `supertest`
- Adding routes, middleware, request validation, or JWT auth
- Structuring controllers/services or wiring error-handling middleware

## Patterns and Best Practices

Structure — keep `app` and `server` separate so tests import `app`:

```
src/
  app.ts            # builds express app, mounts routers + error handler
  server.ts         # app.listen(...)
  config.ts         # env parsing/validation
  middleware/       # auth.ts, errorHandler.ts, validate.ts
  routes/           # users.ts
  controllers/      # users.controller.ts
  services/         # users.service.ts
tests/
  users.test.ts
```

Validated config:

```ts
// config.ts
import { z } from "zod";

const schema = z.object({
  PORT: z.coerce.number().default(3000),
  JWT_SECRET: z.string().min(16),
  DATABASE_URL: z.string().url(),
});
export const config = schema.parse(process.env);
```

App wiring with security middleware:

```ts
// app.ts
import express from "express";
import helmet from "helmet";
import cors from "cors";
import rateLimit from "express-rate-limit";
import { usersRouter } from "./routes/users.js";
import { errorHandler } from "./middleware/errorHandler.js";

export function createApp() {
  const app = express();
  app.use(helmet());
  app.use(cors());
  app.use(express.json());
  app.use(rateLimit({ windowMs: 60_000, max: 100 }));
  app.use("/users", usersRouter);
  app.use(errorHandler); // must be last, 4-arg signature
  return app;
}
```

Zod validation middleware:

```ts
// middleware/validate.ts
import { RequestHandler } from "express";
import { ZodSchema } from "zod";

export const validateBody =
  (schema: ZodSchema): RequestHandler =>
  (req, res, next) => {
    const result = schema.safeParse(req.body);
    if (!result.success) {
      return res.status(400).json({ error: result.error.flatten() });
    }
    req.body = result.data;
    next();
  };
```

Async error handling — wrap handlers so rejected promises reach the error middleware:

```ts
// middleware/asyncHandler.ts
import { RequestHandler } from "express";
export const asyncHandler =
  (fn: RequestHandler): RequestHandler =>
  (req, res, next) =>
    Promise.resolve(fn(req, res, next)).catch(next);
```

```ts
// middleware/errorHandler.ts
import { ErrorRequestHandler } from "express";

export class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export const errorHandler: ErrorRequestHandler = (err, _req, res, _next) => {
  if (err instanceof HttpError) return res.status(err.status).json({ error: err.message });
  console.error(err);
  res.status(500).json({ error: "internal error" });
};
```

JWT auth middleware:

```ts
// middleware/auth.ts
import { RequestHandler } from "express";
import jwt from "jsonwebtoken";
import { config } from "../config.js";
import { HttpError } from "./errorHandler.js";

export const requireAuth: RequestHandler = (req, _res, next) => {
  const header = req.header("authorization") ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : null;
  if (!token) throw new HttpError(401, "missing token");
  try {
    (req as any).userId = (jwt.verify(token, config.JWT_SECRET) as any).sub;
    next();
  } catch {
    throw new HttpError(401, "invalid token");
  }
};
```

Router + controller + service:

```ts
// routes/users.ts
import { Router } from "express";
import { z } from "zod";
import { asyncHandler } from "../middleware/asyncHandler.js";
import { validateBody } from "../middleware/validate.js";
import { requireAuth } from "../middleware/auth.js";
import * as ctrl from "../controllers/users.controller.js";

const createSchema = z.object({ email: z.string().email(), name: z.string().min(1) });
export const usersRouter = Router();
usersRouter.post("/", validateBody(createSchema), asyncHandler(ctrl.create));
usersRouter.get("/me", requireAuth, asyncHandler(ctrl.me));
```

```ts
// controllers/users.controller.ts
import { Request, Response } from "express";
import * as service from "../services/users.service.js";

export async function create(req: Request, res: Response) {
  const user = await service.createUser(req.body);
  res.status(201).json(user);
}
```

Integration tests with supertest:

```ts
// tests/users.test.ts
import request from "supertest";
import { createApp } from "../src/app.js";

const app = createApp();
test("rejects invalid body", async () => {
  const res = await request(app).post("/users").send({ email: "nope" });
  expect(res.status).toBe(400);
});
```

## Anti-patterns

- Forgetting `next(err)` / `asyncHandler` in async routes — rejected promises hang the request in Express 4.
- Putting DB queries and business logic directly in route callbacks instead of a service layer.
- Registering the error handler before routes or with the wrong arity (must be 4 args, last).
- Reading `process.env` scattered across files instead of one validated `config`.
- Skipping `helmet`, `cors`, and rate limiting on public APIs.
- Calling `app.listen` in the same module you import in tests — separate `app` from `server`.
- Trusting `req.body`/`req.params` without schema validation.
