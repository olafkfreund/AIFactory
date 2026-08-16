"""
Spec-Based Stack Inference
==========================

For from-scratch / empty-repo builds the on-disk project has no source files
yet, so ``StackDetector`` finds nothing and the dynamic security allowlist is
missing the language tooling the coder immediately needs (``python``/``pip``/
``pytest``, ``node``/``npm``, ...). The build then stalls because every
language command is blocked.

This module infers the *intended* stack from the spec's own text (``spec.md``,
``requirements.json``, ``context.json``) so the allowlist can be **seeded**
before any code exists. The inferred stack is merged (union, never shrink) into
the detected stack — for existing repos detection already finds the real stack
and this only adds the language tools the spec declares, so there is no
security regression.

See issue #391.
"""

import contextlib
import json
import re
from pathlib import Path

from .models import TechnologyStack

# Files inside the spec dir whose text we scan for stack signals.
_SPEC_TEXT_FILES = ("spec.md", "context.json")
# requirements.json carries the user's description under known keys.
_REQUIREMENTS_TEXT_KEYS = ("title", "description", "task_description")

# Keyword -> canonical stack value. Keys are matched as whole words
# (case-insensitive) against the combined spec text. Order does not matter;
# all matches accumulate. Canonical values MUST match the registry keys in
# project/command_registry/ so _build_stack_commands picks them up.
_LANGUAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "python": ("python", "py3", "fastapi", "django", "flask", "pydantic", "uvicorn"),
    "javascript": ("javascript", "node", "node.js", "nodejs", "express", "npm"),
    "typescript": ("typescript", "ts", "tsx", "nestjs", "next.js", "nextjs"),
    "go": ("golang", "go module", "go mod"),
    "rust": ("rust", "cargo", "rustc"),
    "ruby": ("ruby", "rails", "sinatra"),
    "php": ("php", "laravel", "symfony"),
    "java": ("java", "spring boot", "spring"),
    "csharp": ("c#", "csharp", ".net", "dotnet", "asp.net"),
}

_PACKAGE_MANAGER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pip": ("pip", "requirements.txt"),
    "uv": ("uv", "uvx"),
    "poetry": ("poetry",),
    "pdm": ("pdm",),
    "pipenv": ("pipenv",),
    "npm": ("npm",),
    "yarn": ("yarn",),
    "pnpm": ("pnpm",),
    "bun": ("bun",),
    "cargo": ("cargo",),
    "go_mod": ("go mod", "go.mod"),
    "gem": ("bundler", "gemfile"),
    "composer": ("composer",),
    "maven": ("maven", "pom.xml"),
    "gradle": ("gradle",),
    "nuget": ("nuget",),
}

_FRAMEWORK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fastapi": ("fastapi",),
    "django": ("django",),
    "flask": ("flask",),
    "starlette": ("starlette",),
    "pytest": ("pytest", "py.test"),
    "ruff": ("ruff",),
    "mypy": ("mypy",),
    "black": ("black",),
    "alembic": ("alembic",),
    "celery": ("celery",),
    "react": ("react",),
    "nextjs": ("next.js", "nextjs"),
    "vue": ("vue",),
    "angular": ("angular",),
    "svelte": ("svelte",),
    "express": ("express",),
    "nestjs": ("nestjs", "nest.js"),
    "vite": ("vite",),
    "jest": ("jest",),
    "vitest": ("vitest",),
    "playwright": ("playwright",),
    "cypress": ("cypress",),
    "eslint": ("eslint",),
    "prettier": ("prettier",),
    "prisma": ("prisma",),
    "rails": ("rails",),
    "rspec": ("rspec",),
    "laravel": ("laravel",),
    "phpunit": ("phpunit",),
    "gin": ("gin framework", "gin-gonic"),
    "axum": ("axum",),
    "actix": ("actix",),
    "phoenix": ("phoenix",),
}

# When a language is inferred but the spec does not name its standard tooling,
# seed sensible defaults so trailing test/build gates still work (issue #391:
# "A from-scratch Python/FastAPI build can run python/pip/pytest").
_LANGUAGE_DEFAULTS: dict[str, dict[str, tuple[str, ...]]] = {
    "python": {"package_managers": ("pip",), "frameworks": ("pytest",)},
    "javascript": {"package_managers": ("npm",), "frameworks": ()},
    "typescript": {"package_managers": ("npm",), "frameworks": ()},
    "go": {"package_managers": ("go_mod",), "frameworks": ()},
    "rust": {"package_managers": ("cargo",), "frameworks": ()},
}


def _read_spec_text(spec_dir: Path) -> str:
    """Collect scannable text from the spec directory (best-effort)."""
    parts: list[str] = []

    for name in _SPEC_TEXT_FILES:
        path = spec_dir / name
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue

    req_path = spec_dir / "requirements.json"
    with contextlib.suppress(OSError, json.JSONDecodeError):
        # requirements.json is optional; absent/malformed just means no extra text
        data = json.loads(req_path.read_text(encoding="utf-8", errors="ignore"))
        if isinstance(data, dict):
            for key in _REQUIREMENTS_TEXT_KEYS:
                value = data.get(key)
                if isinstance(value, str):
                    parts.append(value)

    return "\n".join(parts)


def _match_keywords(text: str, keyword_map: dict[str, tuple[str, ...]]) -> list[str]:
    """Return canonical values whose keywords appear as whole words in text."""
    found: list[str] = []
    for canonical, keywords in keyword_map.items():
        for kw in keywords:
            # Whole-word, case-insensitive. re.escape handles '.'/'#'/'+'.
            if re.search(rf"(?<![\w.]){re.escape(kw)}(?![\w])", text, re.IGNORECASE):
                found.append(canonical)
                break
    return found


def infer_stack_from_spec(spec_dir: Path | None) -> TechnologyStack:
    """
    Infer a TechnologyStack from the spec's text.

    Returns an empty stack when no spec dir is given or nothing matches, so the
    caller can safely union it into the detected stack.
    """
    stack = TechnologyStack()
    if not spec_dir:
        return stack

    spec_dir = Path(spec_dir)
    if not spec_dir.exists():
        return stack

    text = _read_spec_text(spec_dir)
    if not text.strip():
        return stack

    languages = _match_keywords(text, _LANGUAGE_KEYWORDS)
    package_managers = _match_keywords(text, _PACKAGE_MANAGER_KEYWORDS)
    frameworks = _match_keywords(text, _FRAMEWORK_KEYWORDS)

    # Seed standard tooling for inferred languages when not explicitly named.
    for lang in languages:
        defaults = _LANGUAGE_DEFAULTS.get(lang, {})
        package_managers.extend(defaults.get("package_managers", ()))
        frameworks.extend(defaults.get("frameworks", ()))

    stack.languages = sorted(set(languages))
    stack.package_managers = sorted(set(package_managers))
    stack.frameworks = sorted(set(frameworks))
    return stack


def merge_stack(base: TechnologyStack, extra: TechnologyStack) -> None:
    """Union `extra` into `base` in place (never removes anything)."""
    for f in (
        "languages",
        "package_managers",
        "frameworks",
        "databases",
        "infrastructure",
        "cloud_providers",
        "code_quality_tools",
        "version_managers",
    ):
        merged = sorted(set(getattr(base, f)) | set(getattr(extra, f)))
        setattr(base, f, merged)
