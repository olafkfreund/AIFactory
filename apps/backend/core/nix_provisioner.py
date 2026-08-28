#!/usr/bin/env python3
"""nix_provisioner — RFC-0005 Tier A (Nix) materialization, shared across the fleet.

Turns an RFC-0005 `environment` manifest (the contract `$defs.environment` block)
into (a) a generated `flake.nix` when the repo carries none, and (b) the
`nix develop -c` argv that materializes that flake and runs the build/verify
commands hermetically. Pairs with `factory_sandbox.py`: the sandbox provides the
*disposable container*, this provides the *toolchain inside it*.

Why a generator, not a hand-written flake per repo: the planner already resolves
language + toolchain + system_packages into the manifest, so the flake is a pure
function of the manifest — generate it, commit it as a deliverable
(`provisioning.generated = true`), and the build env (AIFactory) and verify env
(TFactory) cannot drift because both `nix develop` the same `flake.lock`.

The flake template is the PROVEN PoC recipe (validated 2026-06-17: real Playwright
screenshots from `nix develop -c`):
  - version-matched `playwright-test` (node) + `playwright-driver.browsers` from
    nixpkgs, browsers wired via PLAYWRIGHT_BROWSERS_PATH (no network download);
  - the Nix `playwright` binary is used directly — NEVER `npx playwright`, which
    re-fetches a mismatched copy.

SDK-free, pure-string + argv building so it is testable without Nix installed.
Run directly for the self-tests: `python3 scripts/nix_provisioner.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# nixpkgs pin for generated flakes. A FULL commit rev (not a branch) keeps
# generated flakes reproducible AND avoids a GitHub API call to resolve the
# branch — which anonymously rate-limits inside a token-less k8s-Job container
# (proven 2026-06-17: a branch ref 403'd; the pinned rev fetches the tarball
# directly). Bump deliberately (Renovate can automate).
DEFAULT_NIXPKGS = "github:NixOS/nixpkgs/567a49d1913ce81ac6e9582e3553dd90a955875f"

# Pinned lock metadata for DEFAULT_NIXPKGS (#778). Captured from `nix flake lock`
# in the runner image — NEVER hand-edit: a wrong narHash makes nix REJECT the lock
# and re-lock (or error), so it must come from nix and stay in lockstep with
# DEFAULT_NIXPKGS (test_nix_provisioner pins them together). Every generated flake
# has exactly ONE input (nixpkgs at a full rev), so this ONE lock fits them all —
# shipping it beside the flake stops each ephemeral verify Job from re-locking
# nixpkgs on every run (a per-Job `nix flake lock` roundtrip, #778).
_DEFAULT_NIXPKGS_NARHASH = "sha256-lrp67w8AulE9Ks53n27I45ADSzbOCn4H+CNW1Ck8B+8="
_DEFAULT_NIXPKGS_LASTMODIFIED = 1781577229


def generate_lock(nixpkgs: str = DEFAULT_NIXPKGS) -> str | None:
    """The ``flake.lock`` for a generated flake, or None when the rev is unknown.

    Only emitted for ``DEFAULT_NIXPKGS`` — the single rev whose narHash we captured
    from nix. Any other ``nixpkgs`` returns None so nix resolves + locks it itself
    (correct, just not pre-locked). The lock's ``original`` must match the flake's
    ``inputs.nixpkgs.url`` (a rev-pinned github ref) exactly, or nix re-locks.
    """
    if nixpkgs != DEFAULT_NIXPKGS or not nixpkgs.startswith("github:NixOS/nixpkgs/"):
        return None
    rev = nixpkgs.rsplit("/", 1)[-1]
    github_ref = {"owner": "NixOS", "repo": "nixpkgs", "rev": rev, "type": "github"}
    lock = {
        "nodes": {
            "nixpkgs": {
                "locked": {
                    "lastModified": _DEFAULT_NIXPKGS_LASTMODIFIED,
                    "narHash": _DEFAULT_NIXPKGS_NARHASH,
                    **github_ref,
                },
                "original": dict(github_ref),
            },
            "root": {"inputs": {"nixpkgs": "nixpkgs"}},
        },
        "root": "root",
        "version": 7,
    }
    return json.dumps(lock, indent=2) + "\n"


# language -> (nix python attr | node attr). Extend as the fleet grows.
_PY_ATTR = {
    "3.11": "python311",
    "3.12": "python312",
    "3.13": "python313",
    "3.14": "python314",
}

# common pip/distribution names -> the nixpkgs pythonPackages attr when they
# differ. Anything not here is passed through unchanged (most match).
_PY_PKG_ALIASES = {
    "pyyaml": "pyyaml",
    "beautifulsoup4": "beautifulsoup4",
    "scikit-learn": "scikit-learn",
}

# Curated PyPI-name -> nixpkgs python3Packages attr for deps we seed into a
# generated flake from the SUT's pyproject.toml (#615). Only vetted, stable
# attrs — an unmapped dependency is skipped (logged), never guessed, so a bad
# attr can't break the flake build. Extend as real repos need it.
_PYPROJECT_DEP_MAP = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "starlette": "starlette",
    "httpx": "httpx",
    "pydantic": "pydantic",
    "pydantic-settings": "pydantic-settings",
    "requests": "requests",
    "flask": "flask",
    "aiohttp": "aiohttp",
    "sqlalchemy": "sqlalchemy",
    "jinja2": "jinja2",
    "click": "click",
    "typer": "typer",
    "numpy": "numpy",
    "pandas": "pandas",
    "pyyaml": "pyyaml",
    "python-dateutil": "python-dateutil",
    "pytest": "pytest",
    "pytest-cov": "pytest-cov",
    "pytest-asyncio": "pytest-asyncio",
    "pytest-mock": "pytest-mock",
    "anyio": "anyio",
}


def _dep_base_name(spec: str) -> str:
    """Strip version/extras/markers from a PEP 508 dep string -> lowercase name.

    ``uvicorn[standard]>=0.32`` -> ``uvicorn``; ``httpx>=0.27`` -> ``httpx``.
    """
    return re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0].strip().lower()


def _deps_from_pyproject(project_dir) -> list[str]:
    """Mapped nixpkgs attrs for the SUT's declared deps + test extras (#615).

    Reads ``<project_dir>/pyproject.toml`` ``[project].dependencies`` and any
    ``[project.optional-dependencies]`` ``test``/``dev`` group, maps known names
    via ``_PYPROJECT_DEP_MAP`` and drops the rest. Best-effort: a missing or
    unparseable pyproject yields ``[]`` (the flake still ships the base toolchain).
    """
    pp = Path(project_dir) / "pyproject.toml"
    if not pp.is_file():
        return []
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    proj = data.get("project", {}) if isinstance(data, dict) else {}
    specs = list(proj.get("dependencies", []) or [])
    extras = proj.get("optional-dependencies", {}) or {}
    for group in ("test", "tests", "dev"):
        specs += list(extras.get(group, []) or [])
    out: list[str] = []
    for spec in specs:
        attr = _PYPROJECT_DEP_MAP.get(_dep_base_name(str(spec)))
        if attr and attr not in out:
            out.append(attr)
    return out


class ProvisionError(RuntimeError):
    pass


@dataclass
class Manifest:
    """Typed view over the contract `environment` block (all fields optional)."""

    language: str | None = None
    toolchain: dict[str, str] = field(default_factory=dict)
    system_packages: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    verify_commands: list[str] = field(default_factory=list)
    network: str | None = None
    proof_verify: list[str] = field(default_factory=list)
    provisioning_method: str = "nix"
    provisioning_ref: str | None = None
    provisioning_generated: bool = False

    @classmethod
    def from_contract(cls, env: dict) -> Manifest:
        prov = env.get("provisioning") or {}
        return cls(
            language=env.get("language"),
            toolchain=dict(env.get("toolchain") or {}),
            system_packages=list(env.get("system_packages") or []),
            build_commands=list(env.get("build_commands") or []),
            verify_commands=list(env.get("verify_commands") or []),
            network=env.get("network"),
            proof_verify=list((env.get("proof") or {}).get("verify") or []),
            provisioning_method=prov.get("method", "nix"),
            provisioning_ref=prov.get("ref"),
            provisioning_generated=bool(prov.get("generated", False)),
        )


# RFC-0005 §3.2 provisioning tiers. Resolved from the manifest's
# provisioning.method; the value is the engine that materializes the env.
_TIER_BY_METHOD = {
    "nix": "nix",  # Tier A — hermetic Nix flake (preferred)
    "image": "catalog",  # Tier B — prebuilt catalog image by (language, version)
    "catalog": "catalog",
    "build": "build",  # Tier C — on-demand build, content-addressed + cached
    "on-demand": "build",
    "setup": "setup",  # Tier D — in-container setup script (last resort)
}


def resolve_tier(env: dict) -> str:
    """Resolve the provisioning tier (RFC-0005 §3.2) for a contract environment.

    Returns one of ``nix`` | ``catalog`` | ``build`` | ``setup``. Defaults to
    ``nix`` — the hermetic, content-addressed, any-toolchain tier — so an
    unrecognised method degrades to the reproducible path rather than failing.
    """
    method = (Manifest.from_contract(env).provisioning_method or "nix").lower()
    return _TIER_BY_METHOD.get(method, "nix")


def manifest_digest(env: dict, *, length: int = 16) -> str:
    """Content-addressed digest of a manifest's *environment-defining* fields.

    This is the cache key / image tag that makes Tier B/C "second run is instant"
    explicit: two manifests that describe the same toolchain (same language,
    toolchain versions, system_packages, network class, browser need) hash to the
    same value — regardless of build/verify *commands*, which don't change the
    environment. (Tier A's Nix store content-addresses the build itself; this is
    the manifest-level key the image tiers need.) Deterministic + pure.
    """
    m = Manifest.from_contract(env)
    key = {
        "language": (m.language or "").lower(),
        "toolchain": {k: str(v) for k, v in sorted(m.toolchain.items())},
        "system_packages": sorted(p.lower() for p in m.system_packages),
        "network": m.network or "",
        "browser": _needs_browser(m),
        "nixpkgs": DEFAULT_NIXPKGS if resolve_tier(env) == "nix" else "",
    }
    blob = json.dumps(key, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:length]


def _needs_browser(m: Manifest) -> bool:
    """A browser lane is implied by a browser system package or a playwright/
    chromium reference in the verify commands or proof checks."""
    hay = " ".join(m.system_packages + m.verify_commands + m.proof_verify).lower()
    return any(t in hay for t in ("playwright", "chromium", "browser"))


def _needs_jest(m: Manifest) -> bool:
    """A jest lane is implied by a jest reference in the packages or commands.

    TFactory#1165: the jest lane was the only unit-capable lane with no
    in-cluster path -- its runner was DockerRunner-only, and TFactory pods have
    no container runtime, so EVERY JavaScript/TypeScript unit test errored
    before it started. It read as flakiness; it was total.

    Mirrors :func:`_needs_browser` deliberately: same detection shape, same
    "one token buys the toolchain" contract, so the two node lanes stay
    symmetrical rather than growing separate conventions.
    """
    hay = " ".join(m.system_packages + m.verify_commands + m.proof_verify).lower()
    return "jest" in hay


def _python_attr(m: Manifest) -> str:
    ver = m.toolchain.get("python")
    return _PY_ATTR.get(ver or "", "python313")


# go toolchain version -> nixpkgs attr. Bare `go` tracks the pinned nixpkgs'
# default Go (always present); a requested minor only maps to an explicit
# attr for versions we know exist in the pin, else degrades to `go` rather
# than emitting a non-existent attr (which would fail the flake eval).
_GO_ATTR = {
    "1.21": "go_1_21",
    "1.22": "go_1_22",
    "1.23": "go_1_23",
}


def _go_attr(m: Manifest) -> str:
    ver = m.toolchain.get("go")
    return _GO_ATTR.get(ver or "", "go")


# nixpkgs top-level attrs we know how to map system_packages onto. Browser libs
# come bundled with playwright-driver.browsers, so a bare 'chromium' is dropped
# in favour of the playwright stack (added separately) to avoid version skew.
_DROP_SYSTEM_PKGS = {
    "chromium",
    "playwright",
    "browser",
    "playwright-driver",
    # Same reason as the browser attrs above: `jest` is the TRIGGER token, not a
    # top-level nixpkgs attr. It is dropped here and buys nodejs below; the
    # runner itself comes from npm at lane setup, because nixpkgs has no jest
    # attribute to name (see the jest block in generate_flake).
    "jest",
    # The Python harness already supplies all three via `withPackages` a few
    # lines below, and NONE of them is a top-level nixpkgs attr -- `pkgs.pytest`
    # does not exist (nixpkgs suggests "btest, cpptest, evtest"), so emitting it
    # failed the WHOLE flake eval, not just that package. A manifest naming
    # `pytest` in system_packages is the most obvious thing a planner writes for
    # a Python lane, and it produced a dev shell that could not be realised.
    #
    # Found by the generated-flake evaluation gate in factory-runners, on its
    # first run. Same defect class as the `nodePackages.jest` emission
    # (Factory#1007) and invisible for the same reason: the unit tests assert on
    # the flake's TEXT, and nothing evaluated one.
    "pytest",
    "pip",
    "python",
    # `selenium` is a PyPI/npm package, not a nixpkgs top-level attr — the same
    # shape as `jest` above. Unlike `jest` it does NOT name a toolchain we can
    # infer: selenium bindings exist for Python and for node, and the manifest's
    # `language` already decides which harness runs, so buying node here would
    # be a guess that is wrong half the time (and a ~200MB one). Drop it and let
    # the lane's own package manager install the bindings, exactly as it does
    # for the driver itself.
    "selenium",
}

# Common name a planner writes -> the nixpkgs attr that actually exists at the
# pinned rev (Factory#1009). These differ from _DROP_SYSTEM_PKGS: they name a
# REAL toolchain, so dropping them would silently take the toolchain away rather
# than fix the flake. Each token below was measured against nixpkgs 567a49d1 —
# `pkgs.node`, `pkgs.npm`, `pkgs.rust`, `pkgs.dotnet` and `pkgs.vitest` do not
# exist, and a missing attr fails the WHOLE devShell eval, so the lane reports a
# runner failure instead of a test result (reads as flakiness, is not).
#
#   node/npm -> nodejs_22, the same attr the jest and browser blocks add; npm
#               ships inside the nodejs derivation, so it needs no attr of its own.
#   vitest   -> nodejs_22 too: no nixpkgs attr exists, but unlike selenium it is
#               unambiguously a node runner, so the token buys node and the
#               runner comes from npm at lane setup (the jest contract).
#   rust     -> rustc. `cargo` is already a valid top-level attr and needs no alias.
#   dotnet   -> dotnet-sdk.
#
# Aliasing makes collisions routine — `node` + `npm` is the likeliest pair a
# planner writes, and `vitest` beside a browser lane is another — so
# generate_flake dedupes the final attr list. Measured: a duplicate is NOT an
# eval or build failure (both forms realise and run `node --version`), so this
# is tidiness in a committed deliverable, not correctness.
_SYSTEM_PKG_ALIASES = {
    "node": "nodejs_22",
    "npm": "nodejs_22",
    "vitest": "nodejs_22",
    "rust": "rustc",
    "dotnet": "dotnet-sdk",
}


def _system_pkg_attrs(m: Manifest) -> list[str]:
    return [
        _SYSTEM_PKG_ALIASES.get(p.lower(), p)
        for p in m.system_packages
        if p.lower() not in _DROP_SYSTEM_PKGS
    ]


def generate_flake(env: dict, *, nixpkgs: str = DEFAULT_NIXPKGS, project_dir=None) -> str:
    """Render a reproducible `flake.nix` from an RFC-0005 environment manifest.

    Mirrors the proven PoC: a single devShell with the language toolchain, any
    system packages, and (when a browser lane is implied) version-matched
    playwright-test + browsers wired via env. The shell's tools are on PATH so
    consumers call the Nix binaries directly.
    """
    m = Manifest.from_contract(env)
    lang = (m.language or "python").lower()
    sys_attrs = _system_pkg_attrs(m)

    pkg_lines: list[str] = []
    if lang == "go":
        # Go has no withPackages set — the toolchain is one attr; test/coverage
        # tools (gotestsum, gocover-cobertura) ride in as system_packages.
        pkg_lines.append(f"pkgs.{_go_attr(m)}")
    else:
        py = _python_attr(m)
        py_pkgs = [_PY_PKG_ALIASES.get(p, p) for p in _python_libs(m, project_dir=project_dir)]
        if py_pkgs:
            # Reference each attr as ``p."name"`` (quoted) rather than
            # ``with p; [ name ]`` so hyphenated attrs (pytest-cov,
            # scikit-learn) don't parse as Nix subtraction.
            joined = " ".join(f'p."{name}"' for name in py_pkgs)
            pkg_lines.append(f"(pkgs.{py}.withPackages (p: [ {joined} ]))")
        else:
            pkg_lines.append(f"pkgs.{py}")
    sys_attrs_with_node = list(sys_attrs)
    browser = _needs_browser(m)
    if browser:
        # dejavu_fonts + a FONTCONFIG_FILE so headless chromium actually renders
        # text in a minimal Nix container (without it, screenshots come out
        # textless — proven in-container 2026-06-17).
        sys_attrs_with_node += ["nodejs_22", "playwright-test", "dejavu_fonts"]
    jest = _needs_jest(m)
    # NODE ONLY -- there is no jest attribute, because nixpkgs has none to give.
    #
    # This used to emit `pkgs.nodePackages.jest`. nixpkgs removed the whole
    # `nodePackages` set on 2026-03-03, and it did not degrade to a no-op: the
    # attribute now THROWS, so a manifest naming jest produced a flake that
    # failed to EVALUATE -- taking the entire dev shell down with it, not just
    # jest. The lane then reported a runner failure rather than a test result,
    # which is the same shape as TFactory#1165 and reads as flakiness rather
    # than a missing package. Measured against nixpkgs 567a49d1: forcing the
    # devShell derivation errored with "nodePackages has been removed" before
    # this change and resolves to a .drv after it. Note `nix eval` of the shell
    # NAME passes either way -- the package list is lazy, so a check that does
    # not force the derivation reports a healthy flake that cannot build.
    #
    # The runner comes from npm instead: nix_env.py installs jest/ts-jest/
    # typescript locally, once per lane, and exits 127 loudly if the install or
    # the binary check fails. So node is all the flake owes this lane, and only
    # when the browser block has not already added it. (This guard used to be
    # justified as avoiding a "duplicate-package eval error" -- measured
    # 2026-08-28 against nixpkgs 567a49d1, that is not true: a doubled attr
    # evaluates and realises fine. It is kept because it is still the clearest
    # way to say "the browser block already brought node", and the dedupe below
    # now covers the collisions the alias table makes routine.)
    if jest and not browser:
        sys_attrs_with_node.append("nodejs_22")
    # dict.fromkeys = order-preserving dedupe (see _SYSTEM_PKG_ALIASES): two
    # manifest tokens can alias to the same attr, and the browser and jest
    # blocks add nodejs_22 too. Cosmetic, not load-bearing — a duplicate still
    # evaluates and realises — but the flake is a committed deliverable and a
    # doubled line reads as a bug to the next person.
    for a in dict.fromkeys(sys_attrs_with_node):
        pkg_lines.append(f"pkgs.{a}")

    packages = "\n          ".join(pkg_lines)

    let_lines = ""
    env_lines = ""
    if browser:
        let_lines = (
            "\n      fontsConf = pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; };"
        )
        env_lines = (
            "\n        # Nix-provided, version-matched browsers — no network "
            "download.\n"
            '        PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";\n'
            '        PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";\n'
            '        PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";\n'
            "        # Headless chromium needs fontconfig to find a font, else "
            "text won't render.\n"
            "        FONTCONFIG_FILE = fontsConf;"
        )

    return f"""{{
  # GENERATED by RFC-0005 nix_provisioner from the task contract environment
  # manifest. Committed as a deliverable so build (AIFactory) and verify
  # (TFactory) share one flake.lock and cannot drift. Edit the manifest, not this.
  description = "Factory per-task toolchain (RFC-0005 Tier A)";
  inputs.nixpkgs.url = "{nixpkgs}";
  outputs = {{ self, nixpkgs }}:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {{ inherit system; }};{let_lines}
    in {{
      devShells.${{system}}.default = pkgs.mkShell {{
        packages = [
          {packages}
        ];{env_lines}
      }};
    }};
}}
"""


def _requirements_present(project_dir: Path) -> bool:
    """True when the checkout declares deps in a requirements.txt the lane can install.

    Bounded the same way the runner's discovery is, so this answers the same
    question the Job will ask at run time.
    """
    root = Path(project_dir)
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__"}
    for pattern in ("requirements.txt", "*/requirements.txt", "*/*/requirements.txt"):
        for hit in root.glob(pattern):
            if not any(part in skip for part in hit.relative_to(root).parts):
                return True
    return False


def _python_libs(m: Manifest, project_dir=None) -> list[str]:
    """Python libraries to put in the withPackages set. Always include the verify
    lane's own harness — pytest + pytest-cov (the runner always passes ``--cov``)
    and ``requests`` (the api lane's HTTP client, TFactory#892); add
    fastapi/uvicorn/httpx when the commands imply a web app; and, when
    ``project_dir`` is given, the SUT's own declared deps + test extras read from
    its pyproject.toml (#615) so an ingested repo imports without a hand-written
    manifest."""
    libs: list[str] = []
    hay = " ".join(m.verify_commands + m.build_commands + m.proof_verify).lower()
    # Does this project run a PYTHON harness at all? Reused below: a browser
    # lane only implies a Python web stack when there is Python here to serve
    # with.
    py_harness = (m.language or "").lower() in ("", "python") or "pytest" in hay
    if py_harness:
        # `requests` is HARNESS surface, not app surface, so it rides with pytest
        # rather than waiting on the SUT to declare it. TFactory's pytest
        # descriptor instructs Gen-Functional to drive the api lane with
        # `requests` ("already installed" — true of the legacy docker runner
        # image, false of a generated flake). A SUT that doesn't happen to depend
        # on requests therefore produced an env where EVERY api test died at
        # `import requests`, i.e. at collection, and every HTTP-level acceptance
        # criterion came back unverifiable against correct code (TFactory#892).
        libs += ["pytest", "pytest-cov", "requests"]
    # `_needs_browser` alone is NOT enough. A static HTML/JS page has a browser
    # lane and no Python whatsoever, and this clause was handing it fastapi +
    # uvicorn + httpx purely to serve a static file. That derivation misses the
    # binary cache, builds inside the verify Job's cgroup, and gets OOM-killed:
    # TFactory spec 190/192 each lost a jest verdict to
    #     builder failed due to signal 9 (Killed)
    # on a `python313.withPackages` env the project never needed. The browser
    # lane serves static targets without it (see detect_serve_command).
    #
    # An explicit mention in the commands still pulls the stack in, so a project
    # that really uses it is unaffected -- as is any Python project with a
    # browser lane, which is what this clause was for.
    # The browser clause needs a POSITIVE python signal, not merely "language
    # unset". `py_harness` treats an unset language as python so the pytest
    # harness still lands for manifests that omit it -- correct there, wrong
    # here: a JS spec that declares no language was still buying fastapi +
    # uvicorn + httpx off the back of its browser lane, which is the build that
    # OOM-killed the verify Job (TFactory specs 190/192).
    py_explicit = (m.language or "").lower() == "python" or "pytest" in hay
    if (
        "uvicorn" in hay
        or "fastapi" in hay
        or "httpx" in hay
        or (_needs_browser(m) and py_explicit)
    ):
        libs += ["fastapi", "uvicorn", "httpx"]
    if project_dir is not None:
        libs += _deps_from_pyproject(project_dir)
        # #764: the allowlist above can never be complete, and a repo that
        # declares its deps in requirements.txt gets nothing from it at all.
        # Ship pip so the verify Job can install whatever the map missed — the
        # lane installs into a writable target and prepends it to PYTHONPATH.
        # The allowlist stays: it is the hermetic path when the deps happen to
        # be mapped, and pip only fills the gap.
        if _requirements_present(project_dir):
            libs += ["pip"]
    # de-dup, stable order
    seen: set[str] = set()
    out: list[str] = []
    for x in libs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# `npx <tool>` re-fetches `<tool>` from the npm registry even when the flake has
# already put a version-matched copy on PATH, then runs THAT copy — so the whole
# point of provisioning is silently undone. Proven live: with the flake's
# playwright-test-1.60.0 on PATH, `npx playwright test` reported
# "not found and will be installed: playwright@1.62.1", then died on
# `Cannot find module '@playwright/test'` and found no tests — while
# `playwright test` on the same shell passed in 6.5s.
#
# Only `playwright` is rewritten. The module header states this rule, and the
# flake provably ships that binary whenever a browser lane is implied. Stripping
# `npx` from a tool the flake does NOT ship would turn a working fetch into
# "command not found", which is worse than the bug — so the other npx commands
# discovery emits (mocha, cypress, nyc) are left alone until their toolchains
# are in the flake too.
_NPX_PROVIDED_BY_FLAKE = ("playwright",)

# argv is ["npx", "<tool>", ...]; anything shorter cannot be a rewrite target.
_NPX_MIN_PARTS = 2


def _strip_npx(command: str) -> str:
    """Drop a leading ``npx`` when the flake supplies the tool itself."""
    parts = command.split()
    if len(parts) >= _NPX_MIN_PARTS and parts[0] == "npx" and parts[1] in _NPX_PROVIDED_BY_FLAKE:
        return " ".join(parts[1:])
    return command


def nix_develop_argv(
    flake_dir: str,
    commands: list[str],
    *,
    binary: str = "nix",
    attr: str = "default",
    path_ref: bool = False,
) -> list[str]:
    """argv that materializes `flake_dir`'s devShell and runs `commands` in it.

    `nix develop <dir>#<attr> -c bash -c "<cmd1> && <cmd2>"`. The flake is GC-rooted
    by the store; first run fetches/builds, later runs are cache hits.

    ``path_ref=True`` uses a ``path:`` flake reference instead of the bare dir.
    REQUIRED for a co-mounted git worktree (proven live 2026-06-17): a bare dir
    makes nix use the git fetcher, which (a) rejects the repo on a uid mismatch
    ("repository not owned by current user") and (b) ignores the untracked
    generated flake.nix. ``path:`` copies the path directly — no git, no
    ownership check, untracked flake visible.
    """
    if not commands:
        raise ProvisionError("no commands to run in the nix dev shell")
    joined = " && ".join(_strip_npx(c) for c in commands)
    ref = f"path:{flake_dir}#{attr}" if path_ref else f"{flake_dir}#{attr}"
    return [
        binary,
        "develop",
        ref,
        "--command",
        "bash",
        "-c",
        joined,
    ]


def materialize_or_halt_argv(flake_dir: str, env: dict, **kw) -> list[str]:
    """Build the proof.verify argv (RFC-0005 §3.4). Empty proof => a no-op `true`
    so the caller still gets a runnable command (never a silent skip)."""
    m = Manifest.from_contract(env)
    return nix_develop_argv(flake_dir, m.proof_verify or ["true"], **kw)


# --------------------------------------------------------------------------- #
def _test_npx_strip() -> None:
    """`npx playwright` must become the flake's own binary (TFactory#1152).

    discovery.py emits "npx playwright test" for any repo with a playwright
    config — correct for the docker runner images, fatal on this substrate: npx
    re-fetches a mismatched playwright from npm, that copy cannot resolve
    @playwright/test, and the lane reports zero tests. Every generated browser
    test is then flagged as a runner error and no AC is ever verified.
    """
    argv = nix_develop_argv("/work", ["npx playwright test tests/e2e"])
    assert argv[-1] == "playwright test tests/e2e", argv  # noqa: S101
    # Tools the flake does NOT ship keep their npx — stripping it there would
    # turn a working fetch into "command not found".
    keep = nix_develop_argv("/work", ["npx cypress run", "npx mocha"])
    assert keep[-1] == "npx cypress run && npx mocha", keep  # noqa: S101
    # A bare `npx`, and a command that merely mentions one, are left alone.
    bare = nix_develop_argv("/work", ["npx", "playwright test"])
    assert bare[-1] == "npx && playwright test", bare  # noqa: S101


def _test_go_manifest() -> None:
    """A go env gets the go toolchain and no python at all."""
    # 3b. go manifest -> go toolchain + test/coverage tools, no python/withPackages.
    env_go = {
        "language": "go",
        "toolchain": {"go": "1.22"},
        "system_packages": ["gotestsum", "gocover-cobertura"],
        "verify_commands": ["go test ./..."],
        "provisioning": {"method": "nix", "ref": "flake.nix", "generated": True},
    }
    fg = generate_flake(env_go)
    assert "pkgs.go_1_22" in fg, fg  # noqa: S101
    assert "pkgs.gotestsum" in fg and "pkgs.gocover-cobertura" in fg, fg  # noqa: S101
    assert "withPackages" not in fg and "python" not in fg, fg  # noqa: S101
    assert "pytest" not in fg, fg  # noqa: S101 — no python libs inferred for a go env
    assert "requests" not in fg, fg  # noqa: S101 — harness libs are python-lane only


def _test_browser_lane_deps() -> None:
    """A browser lane implies a python web stack ONLY for a python project."""
    # 3c. javascript + a BROWSER lane -> node, and no python web stack.
    # A static HTML/JS page has a browser lane and no python at all. This clause
    # used to hand it fastapi + uvicorn + httpx purely to serve a static file;
    # that env missed the binary cache, built inside the verify Job's cgroup and
    # was OOM-killed, costing a jest verdict per run (TFactory spec 190/192).
    env_js_browser = {
        "language": "javascript",
        "toolchain": {"node": "22"},
        "system_packages": ["playwright"],
        "verify_commands": ["npx playwright test"],
        "provisioning": {"method": "nix", "ref": "flake.nix", "generated": True},
    }
    fjb = generate_flake(env_js_browser)
    assert "nodejs" in fjb, fjb  # noqa: S101
    assert "fastapi" not in fjb, fjb  # noqa: S101 — nothing here to serve with python
    assert "uvicorn" not in fjb, fjb  # noqa: S101
    assert "withPackages" not in fjb, fjb  # noqa: S101

    # 3d. a PYTHON project with a browser lane still gets the web stack -- that
    # is what the clause was for, and narrowing it must not take that away.
    env_py_browser = {
        "language": "python",
        "toolchain": {"python": "3.13"},
        "system_packages": ["playwright"],
        "verify_commands": ["pytest -q"],
        "provisioning": {"method": "nix", "ref": "flake.nix", "generated": True},
    }
    fpb = generate_flake(env_py_browser)
    assert "fastapi" in fpb, fpb  # noqa: S101
    assert "uvicorn" in fpb, fpb  # noqa: S101


def _test() -> None:
    # 1. browser manifest -> flake has playwright + browsers env, drops bare chromium.
    env_browser = {
        "language": "python",
        "toolchain": {"python": "3.13"},
        "system_packages": ["chromium"],
        "verify_commands": ["pytest -q", "playwright test"],
        "proof": {"verify": ["python --version", "playwright --version"]},
        "provisioning": {"method": "nix", "ref": "flake.nix", "generated": True},
    }
    flake = generate_flake(env_browser)
    assert "python313.withPackages" in flake, flake
    assert "playwright-test" in flake and "nodejs_22" in flake, flake
    assert "PLAYWRIGHT_BROWSERS_PATH" in flake, flake
    assert "pkgs.chromium" not in flake, "bare chromium must be dropped for the pw stack"
    assert "fastapi" in flake and "pytest" in flake, flake  # web+test libs inferred
    # fonts: headless chromium needs them to render text in a minimal container.
    assert "dejavu_fonts" in flake and "FONTCONFIG_FILE" in flake, flake
    assert "makeFontsConf" in flake, flake

    # 2. non-browser python manifest -> no playwright, no browser env.
    env_plain = {
        "language": "python",
        "toolchain": {"python": "3.12"},
        "verify_commands": ["pytest -q"],
        "provisioning": {"method": "nix"},
    }
    f2 = generate_flake(env_plain)
    assert "python312" in f2, f2
    assert "playwright" not in f2 and "PLAYWRIGHT_BROWSERS_PATH" not in f2, f2
    assert "pytest" in f2, f2
    # TFactory#892: the api lane's HTTP client is harness surface — it must be in
    # EVERY python verify env, including one whose SUT never declares it (a plain
    # manifest with no project_dir at all). Without it `import requests` fails at
    # collection and every HTTP acceptance criterion is unverifiable.
    assert 'p."requests"' in f2, f2  # noqa: S101

    # 3. system packages pass through (minus browser drops).
    env_sys = {
        "language": "python",
        "system_packages": ["pkg-config", "openssl"],
        "verify_commands": ["pytest"],
    }
    f3 = generate_flake(env_sys)
    assert "pkgs.pkg-config" in f3 and "pkgs.openssl" in f3, f3

    _test_go_manifest()

    _test_browser_lane_deps()
    # unknown/unset go version degrades to bare `pkgs.go` (no system pkgs here,
    # so `pkgs.go` is the sole package line — no false match on gocover etc.).
    fg2 = generate_flake({"language": "go", "verify_commands": ["go test ./..."]})
    assert "pkgs.go" in fg2 and "pkgs.gocover" not in fg2, fg2  # noqa: S101

    # 4. nix develop argv shape.
    argv = nix_develop_argv("/work", ["pytest -q", "playwright test"])
    assert argv[:3] == ["nix", "develop", "/work#default"], argv
    assert argv[-3:] == ["bash", "-c", "pytest -q && playwright test"], argv
    # path_ref for a co-mounted git worktree (avoids the git-fetcher ownership trap).
    argv_p = nix_develop_argv("/work", ["true"], path_ref=True)
    assert argv_p[2] == "path:/work#default", argv_p
    _test_npx_strip()

    # 5. materialize-or-halt uses proof.verify; empty => true.
    a = materialize_or_halt_argv("/work", env_browser)
    assert a[-1] == "python --version && playwright --version", a
    a0 = materialize_or_halt_argv("/work", env_plain)  # no proof
    assert a0[-1] == "true", a0

    # 6. empty commands rejected.
    try:
        nix_develop_argv("/work", [])
        raise AssertionError("expected ProvisionError")
    except ProvisionError:
        pass

    # 7. generated flake is internally consistent (balanced braces, parseable shape).
    assert flake.count("{") == flake.count("}"), "unbalanced braces in generated flake"

    print("nix_provisioner self-tests: passed")
    # Emit a sample for eyeballing when run with --print.
    import sys

    if "--print" in sys.argv:
        print("\n--- sample generated flake (browser manifest) ---\n")
        print(flake)
        print("--- materialize argv ---")
        print(json.dumps(nix_develop_argv("/work", env_browser["verify_commands"])))


if __name__ == "__main__":
    _test()
