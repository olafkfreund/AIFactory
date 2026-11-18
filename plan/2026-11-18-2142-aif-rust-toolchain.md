---
status: approved
issue: 2142
spec: spec/2026-11-18-2142-aif-rust-toolchain.md
coders:
  2: codex   # small tricky logic, contained edit in nix_env.py
  3: codex   # contained edit in gate_runner.py
reviewers:
  - codex    # final diff review; independent read of detection edge cases
---

# Plan: infer builtin-language toolchains and Java gates (#2142 Rust, #2143 Java)

Self-contained. Approved decisions (spec/2026-11-18-2142-aif-rust-toolchain.md, spec/2026-11-18-2143-aif-java-toolchain.md):

- `core/nix_env.py::infer_environment`: ONE selection followed by shared
  enrichment (no early return on the descriptor path). Collect: `desc` = first
  language with a descriptor (kotlin, swift); builtin set B from detected
  languages and direct markers: rust (Cargo.toml), go (go.mod), node
  (javascript/typescript), java (pom.xml / build.gradle / build.gradle.kts /
  gradlew, root or nested depth<=4), python flag. Primary language = `desc.name`
  if any, else `python` if python is detected and B is non-empty, else B[0]; if
  nothing at all is selected return None (python alone -> None, unknown -> None).
  Every language in B that is not the primary is added as a `system_packages`
  token (rust, go, java, node; rust always also adds `clippy` when rust is in the
  flake). `gradle` is added whenever any Gradle Java module exists (also beside
  Maven). verify_commands come from the primary. A pom
  release/java.version/maven.compiler.release/source > 21 logs a warning.
- Inferred env: `provisioning.generated` is True only if `project_dir/flake.nix`
  does not exist (repo-owned flake preserved). Applies to Kotlin/Swift too.
- `agents/gate_runner.py::detect_gates`: after `_descriptor_gates` has run, add
  Java gates. Independent module discovery: a helper returns every top-most
  directory (depth<=4, skip dirs as `_MODULE_SKIP_DIRS`, do not descend into a
  matched dir) holding `pom.xml` (maven) or a Gradle build file (gradle). One
  gate per dir: `maven-test` (`mvn -B -q test`) / `gradle-test`
  (`gradle test --no-daemon --console=plain`), named plainly for the root and
  `<name>:<reldir>` for nested ones, cwd = that dir. A gradle gate is skipped
  only when an existing gate (e.g. `kotlin-unit`) already runs gradle with the
  same cwd.
- NOT touched: `core/nix_provisioner.py` (byte-checked vendored), qa.py,
  command allowlist, sandbox. Agent-session toolchain is a follow-up.

## Steps

1. tests/test_nix_env_inference.py (extend) and tests/test_gates_java_maven.py
   (new): rust, maven, nested maven, gradle-java, kotlin-priority (java+kotlin
   gradle repo infers kotlin), kotlin+rust union (flake text has rustc, cargo,
   gradle), maven+gradle module repo (flake has maven and gradle), go, node,
   python-only None, python+rust, rust+node union, pom release 25 warns, flake
   text contains rustc/cargo/clippy and jdk21/maven. Preservation test: write a
   sentinel `flake.nix`, call `infer_environment()`, pass ITS result to
   `materialize_flake_into()`, assert the sentinel is unchanged. Gate tests:
   maven gate cwd, nested pom with root build.gradle, sibling Kotlin and Java
   gradle modules both get a gate, same-dir dedupe vs `kotlin-unit`.
   Owner: me. -> verify: new
   tests FAIL on dev (`pytest tests/test_nix_env_inference.py tests/test_gates_java_maven.py`).
   Traps: tests add `apps/backend` to sys.path like the existing file.
2. apps/backend/core/nix_env.py `infer_environment`: implement the builtin pass
   and generated-flag rule. -> verify: inference tests pass.
   Traps: do not import nix_provisioner internals beyond existing imports; keep
   descriptor-first behaviour; ruff `as` alias sorting (plain names).
3. apps/backend/agents/gate_runner.py: maven/gradle gates + marker keys. ->
   verify: gate tests pass, `tests/test_gates_for_descriptor_languages.py` still passes.
4. Run `ruff check` + `ruff format --check` on touched files, the focused tests
   and `tests/test_nix_env_inference.py tests/test_gate_runner_nix.py
   tests/test_gates_for_descriptor_languages.py`. Owner: me.
5. Final: `codex review --base origin/dev`, own read against this plan, PR.

## Tests

`cd apps/backend`-independent: from repo root `pytest tests/test_nix_env_inference.py
tests/test_gates_java_maven.py tests/test_gate_runner_nix.py
tests/test_gates_for_descriptor_languages.py -q` all green.

## Rollback

Revert the PR. Inference falls back to returning None for rust/java (today),
and Java has no gate (today).

## Deviations

- Step 2: split into helpers (`_scan_java`, `_builtin_languages`, `_verify_commands`) for the complexity caps; pom read by regex, not an XML parser (untrusted repo content). Step 3: as planned (module helper in gate_runner.py, no new marker keys).
- Step 2 and 3 were first-cut by codex; the lead reworked step 2 and committed both.
