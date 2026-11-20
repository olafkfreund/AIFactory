"""Remove likely credentials before investigation text is stored."""

import re

_REDACTED = "[REDACTED]"
_SECRET = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    r"|sk-ant-[A-Za-z0-9_-]{10,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    # Base64-ish runs need a digit and both cases and no "/", so long paths survive.
    r"|(?=[A-Za-z0-9+_-]*[0-9])(?=[A-Za-z0-9+_-]*[A-Z])(?=[A-Za-z0-9+_-]*[a-z])"
    r"[A-Za-z0-9+_-]{40,}={0,2}(?![A-Za-z0-9_+/-])"
    r")(?![A-Za-z0-9_+-])"
    r"|(?<![A-Za-z0-9/+])(?=[A-Za-z0-9/+]*[0-9])(?=[A-Za-z0-9/+]*[A-Z])(?=[A-Za-z0-9/+]*[a-z])(?=[A-Za-z0-9/+]*[/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])"
    r"|(?<![A-Fa-f0-9])[A-Fa-f0-9]{40,}(?![A-Fa-f0-9])"
)
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z ]+ )?PRIVATE KEY-----")


def scrub_lines(
    lines: list[str], *, claude_token: str, pem_open: bool
) -> tuple[list[str], bool]:
    """Scrub a batch, carrying an unfinished private key into the next batch."""
    clean = []
    for original in lines:
        if pem_open or _PEM_BEGIN.search(original):
            pem_open = not bool(_PEM_END.search(original))
            continue
        line = original
        if claude_token:
            line = line.replace(claude_token, _REDACTED)
        clean.append(_SECRET.sub(_REDACTED, line))
    return clean, pem_open


def scrub_text(text: str, *, claude_token: str) -> str:
    """Scrub a complete text value, preserving its line endings."""
    lines, _ = scrub_lines(
        text.splitlines(keepends=True), claude_token=claude_token, pem_open=False
    )
    return "".join(lines)
