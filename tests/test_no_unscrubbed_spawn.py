"""Every web-server subprocess spawn must pass a scrubbed ``env=`` (#1680)."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SERVER = _ROOT / "apps" / "web-server" / "server"

# The user's interactive PTY shell is their own shell and keeps its env.
_ALLOW = {"pty/session.py"}

_SPAWN = {
    "subprocess": {"run", "Popen", "check_output", "check_call"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}


def _aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Module aliases (name -> module) and from-imported spawn names."""
    mods: dict[str, str] = {}
    funcs: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in _SPAWN:
                    mods[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module in _SPAWN:
            for a in node.names:
                if a.name in _SPAWN[node.module]:
                    funcs[a.asname or a.name] = (node.module, a.name)
    return mods, funcs


def _is_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    if isinstance(node, ast.Call):
        f = node.func
        return (
            isinstance(f, ast.Attribute) and f.attr == "copy" and _is_environ(f.value)
        )
    if isinstance(node, ast.Dict):
        return any(
            k is None and _is_environ(v)
            for k, v in zip(node.keys, node.values, strict=True)
        )
    return False


# Backend modules the server runs IN-PROCESS (auto-handoff push, workspace
# fetch, PR-endgame diff, merger evidence, plan trust, token lookup). Their
# spawns inherit the server's secrets exactly like the server's own. Not listed,
# because they keep runner-only sites that need the full env: cli/workspace_commands.py
# (merge-preview/commit-message CLI handlers) and agents/gate_runner.py
# (runs a project's own test gates).
_BACKEND = _ROOT / "apps" / "backend"
_BACKEND_FILES = [
    _BACKEND / "core" / "auth.py",
    _BACKEND / "core" / "child_env.py",
    _BACKEND / "core" / "git_credentials.py",
    _BACKEND / "core" / "workspace_fetch.py",
    _BACKEND / "pfactory" / "tfactory_client.py",
    _BACKEND / "trusted_plan.py",
]


def _scanned() -> list[tuple[Path, str]]:
    """(path, key) for every server file and every in-process backend file."""
    server = [
        (p, p.relative_to(_SERVER).as_posix()) for p in sorted(_SERVER.rglob("*.py"))
    ]
    return server + [(p, p.relative_to(_BACKEND).as_posix()) for p in _BACKEND_FILES]


def _offenders() -> list[str]:
    bad: list[str] = []
    for path, rel in _scanned():
        if rel in _ALLOW:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods, funcs = _aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            hit = False
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                mod = mods.get(f.value.id)
                hit = mod is not None and f.attr in _SPAWN[mod]
            elif isinstance(f, ast.Name):
                hit = f.id in funcs
            if not hit:
                continue
            env = next((k.value for k in node.keywords if k.arg == "env"), None)
            if env is None or _is_environ(env):
                bad.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    return bad


def test_no_unscrubbed_spawn() -> None:
    bad = _offenders()
    assert not bad, "unscrubbed spawn sites:\n" + "\n".join(bad)


# Where the scrubbed env is built; everywhere else a whole-environ copy is
# almost always an env about to be handed to a child via a variable, which the
# keyword check above cannot see.
_COPY_ALLOW = _ALLOW | {"core/child_env.py"}


def test_no_environ_copy_outside_helper() -> None:
    bad: list[str] = []
    for path, rel in _scanned():
        if rel in _COPY_ALLOW:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            whole = (isinstance(node, ast.Call | ast.Dict) and _is_environ(node)) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "dict"
                and any(_is_environ(a) for a in node.args)
            )
            if whole:
                bad.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    assert not bad, "whole-environ copies (use child_env):\n" + "\n".join(bad)
