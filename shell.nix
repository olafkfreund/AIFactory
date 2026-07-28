{ pkgs ? import <nixpkgs> {} }:

let
  # CodeQL is used to validate .github/codeql/custom-queries locally before
  # shipping a barrier change (#1044) -- a sanitizer pack that is not measured
  # fails silently in both directions: too narrow suppresses nothing, too broad
  # hides real findings, and the alert count moves either way.
  #
  # It is UNFREE, so it is opt-in rather than a hard dependency: referencing it
  # unconditionally would make this shell fail to evaluate for anyone who has
  # not allowed unfree packages. `lib.optional` is lazy in its value, so
  # pkgs.codeql is never forced when the flag is off.
  #
  # To get it:  NIXPKGS_ALLOW_UNFREE=1 nix-shell --impure
  codeqlIfAllowed = pkgs.lib.optional (pkgs.config.allowUnfree or false) pkgs.codeql;
in
pkgs.mkShell {
  buildInputs = codeqlIfAllowed ++ (with pkgs; [
    python312
    nodejs_24
    just
    git
    gh
    stdenv.cc.cc
    zlib
    libffi
    openssl
  ]);

  shellHook = ''
    # Create the virtualenv if it doesn't exist
    if [ ! -d .venv ]; then
      echo "Creating Python virtualenv..."
      python3 -m venv .venv
    fi
    source .venv/bin/activate

    # Set up the LD_LIBRARY_PATH for compiled Python C-extensions (NixOS dynamic linker fix)
    export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [
      pkgs.stdenv.cc.cc.lib
      pkgs.zlib
      pkgs.libffi
      pkgs.openssl
    ]}:$LD_LIBRARY_PATH"

    # Install python dependencies inside the virtualenv
    if [ ! -f .venv/packages-installed ]; then
      echo "========================================================="
      echo "  AIFactory Native NixOS Development Environment Setup   "
      echo "========================================================="
      echo "  1. Installing Python packages into virtualenv..."
      echo "     (Compiling tree-sitter & LadybugDB C-extensions)"
      echo "---------------------------------------------------------"
      pip install -r apps/backend/requirements.txt -r apps/web-server/requirements.txt

      echo "---------------------------------------------------------"
      echo "  2. Installing Node.js frontend workspace dependencies..."
      echo "---------------------------------------------------------"
      npm ci --workspace=apps/frontend-web

      touch .venv/packages-installed
      echo "---------------------------------------------------------"
      echo "  Setup Complete! Your environment is ready."
      echo "========================================================="
    fi

    echo "========================================================="
    echo "  AIFactory Native Nix Development Shell Active!         "
    echo "========================================================="
    echo "  Python: $(python --version)"
    echo "  Node:   $(node --version)"
    echo "  NPM:    $(npm --version)"
    echo "========================================================="
    echo "  Available Quick Recipes (via 'just'):"
    echo "    - just start   : Start background web-server & frontend"
    echo "    - just stop    : Stop all active processes"
    echo "    - just reload  : Restart the stack"
    echo "    - just logs    : Print and follow logs"
    echo "========================================================="
  '';
}
