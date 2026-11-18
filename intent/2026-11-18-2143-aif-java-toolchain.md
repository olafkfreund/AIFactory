---
status: draft
issue: 2143
author: olafkfreund
---

# Intent: Java/Maven builds get a JDK and Maven, and QA sign-off goes through the framework

## Problem

A Java 21 / Maven / JUnit 5 task (E2E level 5) had no JDK or Maven: coder and QA
logged "No JDK on the machine". QA downloaded Temurin + Maven, then hand-wrote
`qa_signoff` into implementation_plan.json because the framework recorded no
verification.

Root causes on `dev` (closing comments of #1491, #1492, #1608 read first: #1491
fixed descriptor languages only; #1608 established the image's warm store
already holds jdk21/gradle/kotlin, so this is not a store problem):

1. `infer_environment()` returns nothing for `java` (no descriptor), so no flake.
2. `detect_gates()` has no Maven (or Gradle-for-Java) gate. With no gate there is
   no gate evidence, so `update_qa_status(approved)` is refused by
   `_approval_refusal_reason` (#1496) and QA improvises.

## Proposed outcome

A `pom.xml` project gets a flake with jdk21 + maven (gradle added for Gradle
builds), a `mvn -B -q test` gate runs in it, the gate evidence is recorded, and
`update_qa_status(approved)` succeeds without hand-editing the plan.

## Affected users and systems

`core/nix_env.py`, `agents/gate_runner.py`. Java/Maven/Gradle tasks.

## Constraints

- `nix_provisioner.py` is vendored and untouched, so the JDK stays jdk21: it
  compiles any `maven.compiler.release` / `java.version` up to 21. A pom asking
  for more than 21 is logged loudly, not silently mis-provisioned (follow-up on
  the Factory hub to add versioned JDK attrs).
- Out of scope: the 7-minute backlog queue visibility observation (status team).

## Open questions

None. Shares the fix and the PR with #2142.
