"""One redacted, short description of a tool call's input for the task log."""

import json
import re

from factory_common.secrets import redact

MAX_LEN = 100
_SECRET_KEYS = ("password", "token", "secret", "key", "authorization", "credential")
_PEM = re.compile(
    r"-----BEGIN (?P<type>(?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY)-----"
    r".*?-----END (?P=type)-----",
    re.DOTALL,
)
_PREFERRED = ("command", "pattern", "file_path", "path", "url", "query")


def scrub(text: str | None) -> str | None:
    """Remove whole PEM private keys, then known secret shapes."""
    if text is None:
        return None
    return str(redact(_PEM.sub("***REDACTED***", text)))


def _mask(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: "***REDACTED***"
            if any(word in str(k).lower() for word in _SECRET_KEYS)
            else _mask(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_mask(item) for item in value]
    return value


def describe_tool_input(inp: object) -> str | None:
    """Describe *inp* in at most MAX_LEN chars, secrets removed first."""
    if not isinstance(inp, dict) or not inp:
        return None
    text = None
    tail = False
    for key in _PREFERRED:
        value = inp.get(key)
        if isinstance(value, str) and value:
            text = f"pattern: {value}" if key == "pattern" else value
            tail = key == "file_path"
            break
    if text is None:
        text = json.dumps(_mask(inp), default=str, separators=(",", ":"))
    text = scrub(text) or ""
    if len(text) > MAX_LEN:
        # A long path is most useful by its tail.
        text = "..." + text[-(MAX_LEN - 3) :] if tail else text[: MAX_LEN - 3] + "..."
    return text
