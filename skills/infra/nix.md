# nix

> Source: curated best practices | 2026

---

# Nix - Reproducible builds and dev environments with flakes

Nix builds software and development environments reproducibly: pinned inputs (`flake.lock`), pure evaluation, and content-addressed outputs mean the same flake produces the same result on any machine. Flakes are the modern interface — a `flake.nix` declares `inputs` and `outputs` (dev shells, packages, apps, NixOS/container images). This skill covers flake structure, `devShells` for reproducible tooling, packaging with `mkDerivation`, and using Nix to build minimal OCI images.

## When to Activate

Use when the task involves Nix:
- Writing or editing `flake.nix` / `flake.lock`
- Reproducible dev environments (`nix develop`, `devShells`)
- Packaging software as a derivation
- Building container images or per-task environments with Nix
- Pinning toolchains for CI reproducibility

## Patterns and Best Practices

### Flake skeleton — pinned inputs, dev shell, package

```nix
{
  description = "Service with reproducible toolchain";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let pkgs = import nixpkgs { inherit system; };
      in {
        # `nix develop` → identical toolchain for everyone, no global installs
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.python312
            pkgs.uv
            pkgs.ruff
            pkgs.postgresql_16
          ];
          shellHook = ''
            echo "dev shell: python $(python --version)"
          '';
        };

        # `nix build` → the app package
        packages.default = pkgs.python312Packages.buildPythonApplication {
          pname = "myservice";
          version = "1.0.0";
          src = ./.;
          propagatedBuildInputs = with pkgs.python312Packages; [ fastapi uvicorn ];
        };
      });
}
```

`flake.lock` pins every input to an exact revision — commit it so `nix develop`/`nix build` are byte-reproducible across machines and CI. Update deliberately with `nix flake update` (or `nix flake lock --update-input nixpkgs`).

### Packaging a derivation

```nix
pkgs.stdenv.mkDerivation rec {
  pname = "mytool";
  version = "1.2.0";
  src = pkgs.fetchFromGitHub {
    owner = "acme";
    repo  = pname;
    rev   = "v${version}";
    hash  = "sha256-AAAA...=";      # pinned content hash — tamper-evident, reproducible
  };
  nativeBuildInputs = [ pkgs.cmake ];
  meta = with pkgs.lib; {
    description = "A small tool";
    license = licenses.mit;
    platforms = platforms.linux;
  };
}
```

Always pin fetchers with a `hash` — an unpinned fetch is non-reproducible and a supply-chain risk. Prefer explicit `pkgs.foo` over `with pkgs;` at the top level so references stay traceable.

### Minimal OCI images with Nix

```nix
packages.container = pkgs.dockerTools.buildLayeredImage {
  name = "myservice";
  tag = "latest";
  contents = [ self.packages.${system}.default ];
  config = {
    Cmd = [ "/bin/myservice" ];
    User = "10001:10001";          # non-root
  };
};
```

`dockerTools.buildLayeredImage` produces a minimal, reproducible image containing only the closure of what you asked for — no base-OS CVE surface, no package manager. Load with `docker load < $(nix build .#container --print-out-paths)`.

### CI usage

```yaml
- uses: DeterminateSystems/nix-installer-action@main
- uses: DeterminateSystems/magic-nix-cache-action@main
- run: nix flake check          # evaluates outputs, runs checks
- run: nix build .#default
```

`nix flake check` validates all outputs; the `flake.lock` guarantees CI uses the exact toolchain developers used. Use a binary cache (Cachix / magic-nix-cache) so CI doesn't rebuild the world.

### Running builds where there's no container runtime

To run a build/test inside a pod that lacks a Docker daemon, execute Nix directly (`nix build`/`nix develop -c ...`) against a per-task flake — the flake *is* the environment, so no container runtime is required. Pin `nixpkgs` per task for isolation.

## Anti-patterns

- Not committing `flake.lock` — loses reproducibility, the entire point of flakes.
- Unpinned fetchers (`fetchurl`/`fetchFromGitHub` without a `hash`) — non-reproducible, unverified.
- `with pkgs;` at top level — obscures where names come from; prefer explicit `pkgs.foo`.
- `--impure` / reading ambient env or `$HOME` in evaluation — breaks purity and reproducibility.
- Mutable channels instead of flake inputs — drift between machines.
- Rebuilding everything in CI with no binary cache — slow, expensive.
- Hardcoding absolute paths instead of `${pkgs.foo}/bin/...` — non-portable.
- `nix-env -i` imperative installs alongside declarative flakes — mixes paradigms, loses reproducibility.
