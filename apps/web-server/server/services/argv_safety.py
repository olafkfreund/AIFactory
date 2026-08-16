"""Validators for caller-supplied values that reach a subprocess argv.

Every subprocess in this package uses the list form and never ``shell=True``,
so there is no shell to escape and no metacharacter to worry about. What is
left is ARGUMENT injection: a value that starts with ``-`` is read by the
program as an option rather than an operand. That is not cosmetic --
``git log --output=<file>`` turns an unvalidated ref into an arbitrary file
write, which is exactly what ``routes/changelog.py`` was exposed to before
these helpers existed.

Rejections are raised as ``server.error_ref.InputRejectedError`` -- developer-written
text about the caller's own input, which ``client_error`` is allowed to hand
back verbatim instead of hiding behind a correlation id. It subclasses
``ValueError``, so every existing ``except ValueError`` handler is unchanged.

Each helper RETURNS the validated value, so the call site reads::

    base = assert_safe_git_ref(base, "base")

That shape is deliberate twice over: it makes the check impossible to write
and then forget to use, and it is the shape a CodeQL dataflow barrier can
recognise (see ``.github/codeql/custom-queries/CommandInjectionSanitized.ql``).

Scope, stated plainly, because a barrier that claims more than it delivers
hides real findings: these establish that a value is safe as an OPERAND of a
list-form argv. They do NOT make a value safe to interpolate into a shell
command string, and they do NOT make it safe to use as the program name --
the caller still chooses the program in that case, and no amount of validating
the string changes that.
"""

from __future__ import annotations

import re

from server.error_ref import InputRejectedError

# A git ref must start with an alphanumeric, so it can never be parsed as an
# option, and may then use only characters git itself accepts in a ref name.
# Length is bounded well under any filesystem limit.
_GIT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@/+^~{}-]{0,254}")


def assert_safe_git_ref(ref: str, field: str = "ref") -> str:
    """Return ``ref`` if it is a plausible git ref, else raise ``InputRejectedError``.

    Allowlist, not denylist. The load-bearing rule is the first character:
    a ref that cannot begin with ``-`` cannot be parsed by git as an option.
    ``..`` is rejected because callers join refs into ``a..b`` ranges, and an
    embedded separator lets one field rewrite the range it lands in.
    """
    if not isinstance(ref, str) or not _GIT_REF.fullmatch(ref) or ".." in ref:
        raise InputRejectedError(f"Invalid {field}: must be a plain git ref")
    return ref


def assert_not_option(value: str, field: str = "value") -> str:
    """Return ``value`` if it is usable as a positional operand, else raise.

    For a list-form argv the only way a value changes the meaning of the
    command is by being read as an option, so a leading ``-`` is the whole
    check. NUL is rejected too: ``subprocess`` raises on it anyway, and doing
    it here turns a 500 into a 400.
    """
    if not isinstance(value, str) or value.startswith("-") or "\x00" in value:
        raise InputRejectedError(f"Invalid {field}: must not start with '-'")
    return value


def bounded_count(value: object, maximum: int, field: str = "count") -> int:
    """Return ``value`` coerced to an int in ``1..maximum``, else raise.

    Returning an ``int`` means no caller-supplied string reaches argv at all.
    """
    try:
        count = int(str(value))
    except (TypeError, ValueError):
        raise InputRejectedError(f"Invalid {field}: must be an integer") from None
    if count < 1 or count > maximum:
        raise InputRejectedError(f"Invalid {field}: must be between 1 and {maximum}")
    return count
