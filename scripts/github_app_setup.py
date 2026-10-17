"""Register a GitHub App and probe its Copilot review permission."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any, TextIO, cast

_MAX_MESSAGE = 300


def _say(*args: object, file: TextIO | None = None) -> None:
    print(*args, file=file or sys.stdout)


def load_manifest(workflows: bool, name: str | None) -> dict[str, Any]:
    manifest = cast(
        dict[str, Any],
        json.loads(Path(__file__).with_name("github-app-manifest.json").read_text()),
    )
    if workflows:
        manifest["default_permissions"]["workflows"] = "write"
    if name is not None:
        manifest["name"] = name
    return manifest


def form_html(manifest: dict[str, Any], org: str | None) -> str:
    if org is not None and not re.fullmatch(r"[A-Za-z0-9-]+", org):
        raise ValueError("invalid organization name")
    url = (
        f"https://github.com/organizations/{org}/settings/apps/new"
        if org
        else "https://github.com/settings/apps/new"
    )
    value = html.escape(json.dumps(manifest))
    return (
        '<!doctype html>\n<html lang="en"><body>\n'
        f'<form method="post" action="{url}">\n'
        f'<input type="hidden" name="manifest" value="{value}">\n'
        '<button type="submit">Create GitHub App</button>\n'
        "</form>\n</body></html>\n"
    )


def secret_yaml(
    pem: str, name: str = "aifactory-github-app", key: str = "private-key"
) -> str:
    lines = "\n".join(f"    {line}" for line in pem.splitlines())
    return (
        "apiVersion: v1\nkind: Secret\nmetadata:\n"
        f"  name: {name}\n"
        "type: Opaque\nstringData:\n"
        f"  {key}: |\n{lines}\n"
    )


def write_private(path: str | Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(text)


def copilot_listed(status: int, body: dict[str, Any]) -> bool:
    return HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES and any(
        "copilot" in reviewer.get("login", "").lower()
        for reviewer in (body.get("requested_reviewers") or [])
        if isinstance(reviewer, dict) and isinstance(reviewer.get("login"), str)
    )


def convert(code: str, output: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        _say("invalid code", file=sys.stderr)
        return 1
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions",
        headers={"Accept": "application/vnd.github+json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https URL
            body = json.load(response)
    except urllib.error.HTTPError as error:
        _say(f"HTTP {error.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError):
        _say("conversion failed", file=sys.stderr)
        return 1
    if (
        not isinstance(body, dict)
        or not body.get("id")
        or not isinstance(body.get("pem"), str)
        or not body["pem"]
        or not isinstance(body.get("html_url"), str)
        or not body["html_url"]
    ):
        _say("invalid conversion response", file=sys.stderr)
        return 1
    try:
        write_private(output, secret_yaml(body["pem"]))
    except FileExistsError:
        _say("output file already exists", file=sys.stderr)
        return 1
    _say(f"app id: {body['id']}")
    _say(f"slug: {body.get('slug', '')}")
    _say(f"install URL: {body['html_url']}/installations/new")
    _say(
        "after install, the installation id is the number at the end of the settings URL; "
        "apply the file with kubectl yourself"
    )
    return 0


def probe_copilot(
    repo: str, pr: str, app_id: str, installation_id: str, key_file: str
) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not all(
        value.isascii() and value.isdecimal() for value in (pr, app_id, installation_id)
    ):
        _say("invalid repo, PR, or id", file=sys.stderr)
        return 1

    import httpx  # noqa: PLC0415 - only needed for the live probe
    from jose import jwt  # type: ignore[import-untyped]  # noqa: PLC0415 - only needed for the live probe

    try:
        now = int(time.time())
        claims = {"iat": now - 60, "exp": now + 540, "iss": app_id}
        signed = str(jwt.encode(claims, Path(key_file).read_text(), algorithm="RS256"))
        token_response = httpx.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {signed}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
        token_response.raise_for_status()
        token = token_response.json()["token"]
        response = httpx.post(
            f"https://api.github.com/repos/{repo}/pulls/{pr}/requested_reviewers",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"reviewers": ["copilot-pull-request-reviewer[bot]"]},
            timeout=30,
        )
        body = response.json()
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        _say("probe failed", file=sys.stderr)
        return 1

    listed = copilot_listed(
        response.status_code, body if isinstance(body, dict) else {}
    )
    _say(f"HTTP {response.status_code}; copilot_listed: {listed}")
    if listed:
        return 0
    _say(
        "the App token cannot request Copilot; configure a ruleset that requests "
        "Copilot review (docs/docs/concepts/github-app.md)"
    )
    message = body.get("message", "") if isinstance(body, dict) else ""
    _say(str(message)[:_MAX_MESSAGE])
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    manifest_parser = commands.add_parser(
        "manifest", help="write the App registration page"
    )
    manifest_parser.add_argument("--out", required=True)
    manifest_parser.add_argument("--org")
    manifest_parser.add_argument("--workflows", action="store_true")
    manifest_parser.add_argument("--name")
    convert_parser = commands.add_parser(
        "convert", help="exchange a manifest code for an App key"
    )
    convert_parser.add_argument("code")
    convert_parser.add_argument("--out", required=True)
    probe_parser = commands.add_parser(
        "probe-copilot", help="test App access to Copilot reviews"
    )
    for flag in ("repo", "pr", "app-id", "installation-id", "key-file"):
        probe_parser.add_argument(f"--{flag}", required=True)
    args = parser.parse_args(argv)

    if args.command == "convert":
        return convert(args.code, args.out)
    if args.command == "probe-copilot":
        return probe_copilot(
            args.repo, args.pr, args.app_id, args.installation_id, args.key_file
        )
    try:
        Path(args.out).write_text(
            form_html(load_manifest(args.workflows, args.name), args.org)
        )
    except ValueError as error:
        _say(error, file=sys.stderr)
        return 1
    _say(
        f"open {args.out}, create the App, then copy code= from the redirect URL "
        "(valid one hour) and run: convert CODE --out secret.yaml"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
