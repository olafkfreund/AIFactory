---
status: approved
issue: 2142
intent: intent/2026-11-18-2142-aif-rust-toolchain.md
---

# Spec: infer a toolchain for builtin languages (Rust part; shared with #2143)

## Design

Both issues share one mechanism (spec/2026-11-18-2143-aif-java-toolchain.md carries the Java detail).

1. `core/nix_env.py` `infer_environment()`: keep the descriptor pass first
   (kotlin, swift). If it finds nothing, run a second pass over the detected
   languages with a small table `_BUILTIN_INFERENCE` (rust, java, go,
   javascript/typescript). Python is deliberately absent: it keeps today's
   behaviour (default harness / venv), and a test pins that.
   - Multi-language repos (Codex P1): the flake must carry the toolchain of EVERY
     detected builtin language, because gates for all of them run in it. Primary
     language = first descriptor language, else python if detected (keeps the
     pytest harness), else the first builtin one. Every other detected builtin
     language (rust, go, java, node) is added as a `system_packages` token
     (`rust`->rustc+cargo via _LANG_ATTRS, `java`->jdk21+maven, `go`, `node`).
     Python alone still infers nothing.
   - rust: `language: rust`, `system_packages: [clippy]` (the provisioner's
     rust mapping gives rustc+cargo; clippy rides in as a plain attr, so the
     vendored provisioner is untouched), verify `cargo test --quiet`.
   - go: `language: go`, verify `go test ./...`.
   - javascript/typescript: `language: javascript`, verify `npm test --if-present`.
2. Inferred environments are `generated: true` only when the project has no
   `flake.nix`; otherwise `generated: false`, so `materialize_flake_into`
   respects the repo-owned flake (Codex P1). This also changes Kotlin/Swift
   inference to stop clobbering a repo flake: intended.
3. No change to the sandbox, the command allowlist (cargo, mvn, gradle, java
   already allowed in `project/command_registry/languages.py`) or the
   provisioner. The gate path runs outside the agent command hook, so `nix` is
   not admitted to the agent allowlist.

Decision (owner delegation 2026-11-18): flake over bigger image / downloads;
least risky because the path already exists and Kotlin already uses it.

## Alternatives rejected

- Bake rust into the warm image: size, per-language images (#1492 analysis).
- Edit `nix_provisioner.py` (versioned JDK, clippy in _LANG_ATTRS): needs a hub
  change plus re-vendor and pin bump; follow-up, not needed for the outcome.
- Run the coding agent itself inside the flake: larger architectural change
  (the agent's bash is a control-plane pod process); follow-up.

## Risks

- Gate still needs `AIFACTORY_SANDBOX_BACKEND=nixjob`, an image and workspace
  transport (gate_runner.py ~570-627); otherwise it falls back as today.
- Cold fetch: rust/go/node closures are not in the warm image. After a gate
  Job's first use they fetch from cache.nixos.org (about 2 GiB for rust per
  #1608 notes); a rebuilt warm image would remove it (owner/infra decision).
- The coding agent still lacks cargo during subtasks (explicit non-goal).

## Verification

Unit tests (fail before, pass after): inference per language (rust, maven,
gradle-java, kotlin priority, go, node, python unchanged, unknown none);
generated flake carries rustc, cargo, clippy; repo-owned flake preserved.
`pytest tests/test_nix_env_inference.py tests/test_gates_for_descriptor_languages.py`
plus the repo lint (ruff).
