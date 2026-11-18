---
status: draft
issue: 2143
intent: intent/2026-11-18-2143-aif-java-toolchain.md
---

# Spec: Java/Maven toolchain and a gate that records verification

## Design

1. Inference (see spec/2026-11-18-2142-aif-rust-toolchain.md): `java` entry in `_BUILTIN_INFERENCE`.
   - Maven (`pom.xml`): `language: java` (provisioner maps to jdk21 + maven),
     verify `mvn -B -q test`.
   - Gradle (`build.gradle`/`build.gradle.kts`/`gradlew`, no pom):
     `system_packages: [gradle]`, verify `gradle test --no-daemon --console=plain`.
   - The pom's `maven.compiler.release`/`java.version`/`maven.compiler.source`
     is read; above 21 logs a warning (jdk21 cannot build it) and below is fine
     (jdk21 `--release` covers 8..21).
2. `agents/gate_runner.py` `detect_gates()`: add `maven-test`
   (`mvn -B -q test`) when `pom.xml` exists, and `gradle-test` for a Gradle
   Java project not already covered by the kotlin descriptor gate (name
   `kotlin-unit`); built from the module marker so nested modules work via the
   existing `_module_dir_for` helper (add a `java` marker use, which exists).
3. QA sign-off: no code change in qa.py. The approval guard already accepts
   executed gate evidence (#1496); the gap was that Java produced no gate. With
   the gate recorded, `update_qa_status(approved)` passes, so no hand-written
   `qa_signoff`.

## Alternatives rejected

- Relax the QA guard when a gate is absent: re-opens #1496 (unverified PRs).
- Versioned JDK in the flake: needs the vendored provisioner (follow-up).

## Risks

- Maven fetches dependencies at run time: needs Maven Central egress (build
  Job egress admits public 443 per kotlin.yaml note, measured 2026-09-18).
- A new gate can fail builds that previously "passed" with no gate: intended,
  and it is exactly what #1491 asked for.

## Verification

Tests: `detect_gates` on a pom project returns `maven-test`; Gradle java
returns `gradle-test`; kotlin project does not double up; the generated flake
for an inferred Maven env contains jdk21 and maven; pom release > 21 warns.
