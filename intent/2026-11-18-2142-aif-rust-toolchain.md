---
status: approved
issue: 2142
author: olafkfreund
---

# Intent: Rust builds get cargo and rustc from the per-task Nix flake

## Problem

A Rust task (E2E level 4) ran with no `cargo`/`rustc`: subtask-1-1 died with
"cargo: command not found", `nix`/`nix-env` are blocked by the command
allowlist, the bwrap sandbox cannot create a namespace, and the agent
curl-installed rustup into TMPDIR in every subtask (19 min, 9.4M tokens, 3.52
USD for ~100 lines). Verification ran with the sandbox disabled.

Root cause (read from code on `dev`): the build only gets a per-task flake
when the contract carries an `environment`, or `infer_environment()` finds a
language with a *descriptor* (kotlin, swift). Rust has a builtin attr table in
`nix_provisioner._LANG_ATTRS` but no descriptor, so a task created without a
contract (create-and-run) infers nothing, writes no `flake.nix`, and the gate
lane has no toolchain. Same gap class as closed #1491/#1496, which only fixed
descriptor languages.

## Proposed outcome

A repo with `Cargo.toml` and no contract environment gets a generated flake
with rustc, cargo and clippy, and its `cargo test` gate (the verification of
record, run by the framework outside the agent command hook) executes inside it
when the `nixjob` backend is active. Scope is that gate path: the coding
agent's own bash during subtasks is not given the toolchain by this change
(`coder.py` materializes the flake only at trailing-gate time). Making agent
sessions run inside the flake is a separate, larger change, recorded as a
follow-up, not promised here.

## Affected users and systems

AIFactory build Jobs on the `nixjob` sandbox backend (`core/nix_env.py`,
`agents/coder.py` trailing gates). Tasks on Rust projects.

## Constraints

- `core/nix_provisioner.py` is byte-checked against the Factory hub: not edited here.
- No bigger base image, no per-subtask downloads; use the existing flake path.
- The sandbox and command allowlist stay on; nothing is disabled.
- An inferred flake must never overwrite a repository-owned `flake.nix`.
- Out of scope: status/phase reporting (team aif-status).

## Open questions

None. Codex review 2026-11-18 (P1 x2, P2 x3) folded in: scope narrowed, repo flake
preserved, clippy via system_packages, descriptor languages keep priority. Decision (owner delegation 2026-11-18): provision via the per-task flake,
extend inference to builtin languages. Shared with #2143 (one PR).
