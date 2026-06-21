/**
 * Guard test for frontend TypeScript strictness (#648).
 *
 * tsconfig.json's `compilerOptions` is the frontend's type-safety contract.
 * It is trivially easy to weaken accidentally (flip a flag to `false`, drop a
 * line in a merge) and such a regression would silently let untyped/dead code
 * back in without any other gate noticing. This test pins the strictness flags
 * the project has committed to so any weakening fails CI loudly.
 *
 * To intentionally tighten a flag: update tsconfig.json AND this snapshot.
 * To loosen one: don't — that is the whole point of this test.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const tsconfigPath = resolve(here, "../../tsconfig.json");

/**
 * tsconfig.json contains `//` comments (JSONC). Strip them so JSON.parse can
 * read it. Block comments are not used in this file; line comments are.
 */
function readJsonc(path: string): Record<string, unknown> {
  const raw = readFileSync(path, "utf8");
  const noComments = raw
    .split("\n")
    .map((line) => {
      // Only strip `//` that is not inside a string. The tsconfig never puts a
      // `//` inside a value string, so a simple "first // not preceded by :" is
      // unnecessary; a leading-whitespace `//` line strip plus trailing strip
      // covers every comment in this file.
      const idx = line.indexOf("//");
      if (idx === -1) return line;
      // Guard: don't cut a `https://`-style token (none exist here, but be safe).
      const before = line.slice(0, idx);
      if (/[a-z]:$/i.test(before.trimEnd())) return line;
      return before;
    })
    .join("\n");
  return JSON.parse(noComments) as Record<string, unknown>;
}

describe("frontend tsconfig strictness contract (#648)", () => {
  const tsconfig = readJsonc(tsconfigPath);
  const compilerOptions = tsconfig.compilerOptions as Record<string, unknown>;

  // These MUST stay enabled. Loosening any of them weakens the type bar.
  const REQUIRED_TRUE = [
    "strict",
    "noUnusedLocals",
    "noUnusedParameters",
    "noImplicitOverride",
    "noImplicitReturns",
    "noFallthroughCasesInSwitch",
  ] as const;

  it.each(REQUIRED_TRUE)("keeps compilerOptions.%s === true", (flag) => {
    expect(compilerOptions[flag]).toBe(true);
  });

  it("targets at least ES2022", () => {
    expect(compilerOptions.target).toBe("ES2022");
  });

  it("never re-opens the strict holes (#648)", () => {
    // noImplicitAny was previously set to false; it must never reappear as false.
    expect(compilerOptions.noImplicitAny).not.toBe(false);
  });
});
