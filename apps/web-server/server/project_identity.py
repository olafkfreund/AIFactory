"""Pure project slug and repository identity helpers."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SLUG_MAX = 64


def derive_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:56].rstrip("-") or "project"


def validate_slug(slug: str) -> bool:
    return 1 <= len(slug) <= SLUG_MAX and SLUG_RE.fullmatch(slug) is not None


def unique_slug(base: str, taken: set[str]) -> str:
    base = base[:SLUG_MAX].rstrip("-")
    if base not in taken:
        return base
    number = 2
    while True:
        suffix = f"-{number}"
        candidate = f"{base[: SLUG_MAX - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in taken:
            return candidate
        number += 1


def normalize_repo_identity(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url or url.startswith(("/", ".", "~", "file://")):
        return None

    if url.startswith("git@") and ":" in url:
        host, _, path = url[4:].partition(":")
    else:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https", "ssh", "git"}:
                return None
            host, path = parsed.hostname, parsed.path
        except ValueError:
            return None

    path = path.rstrip("/").lower().removesuffix(".git")
    if not host or not path.strip("/"):
        return None
    return f"{host.lower()}/{path.strip('/')}"
