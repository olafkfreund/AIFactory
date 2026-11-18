---
status: draft
issue: 2143
spec: spec/2026-11-18-2143-aif-java-toolchain.md
---

# Plan: infer builtin-language toolchains and Java gates (#2142 Rust, #2143 Java)

Self-contained. Approved decisions (spec/2026-11-18-2142-aif-rust-toolchain.md, spec/2026-11-18-2143-aif-java-toolchain.md):

- `core/nix_env.py::infer_environment`: descriptor pass first (unchanged). If
  none, builtin pass. Primary language = first descriptor language, else
  `python` if detected (only when other builtin languages exist), else first
  builtin. Every OTHER detected builtin language is added as a `system_packages`
  token: rust, go, java, node. Rust also adds `clippy`. Python alone -> None.
- Java is detected by direct markers (pom.xml, build.gradle, build.gradle.kts,
  gradlew; root or nested depth<=4) as well as StackDetector. Gradle-only Java
  adds `gradle` to system_packages. A pom release/java.version/
  maven.compiler.release/source > 21 logs a warning.
- Inferred env: `provisioning.generated` is True only if `project_dir/flake.nix`
  does not exist (repo-owned flake preserved). This applies to Kotlin/Swift too.
- `agents/gate_runner.py::detect_gates`: `maven-test` (`mvn -B -q test`, cwd =
  pom dir) and `gradle-test` (`gradle test --no-daemon --console=plain`, cwd =
  gradle module), via new `_MODULE_MARKERS_BY_LANGUAGE` keys `maven`, `gradle`;
  `gradle-test` skipped when an existing gate already runs gradle in that cwd.
- NOT touched: `core/nix_provisioner.py` (byte-checked vendored), qa.py,
  command allowlist, sandbox. Agent-session toolchain is a follow-up.

coders:
  2: codex   # small tricky logic, contained edit in nix_env.py
  3: codex   # contained edit in gate_runner.py
reviewers:
  - codex    # final diff review; reasons: independent read of detection edge cases

## Steps

1. tests/test_nix_env_inference.py (extend) and tests/test_gates_java_maven.py
   (new): rust, maven, nested maven, gradle-java, kotlin-priority (java+kotlin
   gradle repo infers kotlin), go, node, python-only None, python+rust, rust+node
   union, repo-owned flake preserved, pom release 25 warns, flake text contains
   rustc/cargo/clippy and jdk21/maven; gate tests: maven gate cwd, nested pom with
   root build.gradle, gradle dedupe vs kotlin-unit. Owner: me. -> verify: new
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
