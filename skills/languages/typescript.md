# typescript

> Source: curated best practices | 2026

---

# TypeScript - Strictly-typed, modern ESM application code

This skill equips the coder to write TypeScript 5+ under `strict` mode targeting modern Node.js (20+) or the browser, using ES modules, discriminated unions for domain modeling, and inference-first typing. It assumes `tsconfig` with `strict: true`, ESLint + Prettier, and Vitest (or Jest) for tests. It enforces `unknown` over `any`, exhaustive `switch` handling, and narrow public API surfaces.

## When to Activate

Use when the task involves TypeScript:
- Writing or modifying `.ts`/`.tsx` files, Node services, or browser code
- Frontend (React/Vue/Svelte) or backend (Express/Fastify/NestJS) in TS
- Anything referencing `tsconfig.json`, `package.json`, `eslint`, `vitest`, `jest`, `tsx`

## Idioms and Best Practices

**tsconfig baseline** - non-negotiable strictness:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "verbatimModuleSyntax": true,
    "skipLibCheck": true
  }
}
```
`noUncheckedIndexedAccess` forces you to handle `arr[i]` possibly being `undefined` - keep it on.

**Model domains with discriminated unions**, not optional-field soup:
```ts
type Result<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

function parse(s: string): Result<number> {
  const n = Number(s);
  return Number.isNaN(n) ? { ok: false, error: `bad number: ${s}` } : { ok: true, value: n };
}
```
Callers narrow on the tag; the compiler enforces both branches.

**Exhaustiveness with `never`:**
```ts
function area(shape: Circle | Square): number {
  switch (shape.kind) {
    case "circle": return Math.PI * shape.r ** 2;
    case "square": return shape.side ** 2;
    default: {
      const _exhaustive: never = shape; // compile error if a case is missed
      throw new Error(`unhandled: ${_exhaustive}`);
    }
  }
}
```

**Prefer inference; annotate boundaries.** Let TS infer locals and returns of small functions; explicitly type function parameters, exported function signatures, and public data shapes. Use `type` for unions/aliases and `interface` for object shapes that may be extended.

**`unknown`, never `any`.** At untyped boundaries (JSON, `fetch`), accept `unknown` and validate with a schema library (`zod`) or a hand-written type guard:
```ts
function isUser(x: unknown): x is User {
  return typeof x === "object" && x !== null && "id" in x;
}
```

**Async:** always `await` promises or explicitly `void` them; never leave floating promises (ESLint `no-floating-promises`). Use `Promise.all` for independent concurrent work, `Promise.allSettled` when partial failure is acceptable.

**Immutability:** use `readonly` and `as const` for literals; avoid mutating function arguments.
```ts
const ROLES = ["admin", "user"] as const;
type Role = (typeof ROLES)[number]; // "admin" | "user"
```

**Utility types:** `Pick`, `Omit`, `Partial`, `Record`, `ReturnType`, `Awaited` - reach for these before writing mapped types by hand.

**Testing (Vitest):**
```ts
import { describe, it, expect } from "vitest";
import { parse } from "./parse.js";

describe("parse", () => {
  it("returns ok for a number", () => {
    expect(parse("42")).toEqual({ ok: true, value: 42 });
  });
  it("returns error for junk", () => {
    const r = parse("x");
    expect(r.ok).toBe(false);
  });
});
```
Note ESM `.js` import specifiers even for `.ts` sources under NodeNext.

**Tooling:** Prettier for formatting, `typescript-eslint` with `strictTypeChecked` for lint. Run `tsc --noEmit` in CI. Use `tsx` to run TS directly in dev, build with `tsc` or `esbuild`/`tsup` for libraries.

**Error handling:** throw `Error` subclasses, not strings; attach context. Prefer returning `Result` unions for expected failures and throwing only for programmer errors / unexpected states.

## Anti-patterns

- `any` anywhere it can be avoided - use `unknown` + narrowing or generics.
- Type assertions (`as Foo`) to silence the compiler instead of validating.
- `enum` for simple string sets - use `as const` unions (smaller, no runtime cost).
- Floating promises and unhandled rejections.
- `!` non-null assertions scattered to dodge `strictNullChecks`.
- `namespace` and CommonJS `require` in new code - use ES modules.
- Overusing decorators/reflection-heavy patterns when plain functions suffice.
- Deeply nested conditional/mapped types that no one can maintain.
