"""Client-safe error reporting: a correlation id to the caller, the detail to the log.

CWE-209 / ``py/stack-trace-exposure``. Handlers across this server did:

    except Exception as e:
        return {"success": False, "error": str(e)}

and that string is a response body. It is written by third-party libraries and by
the stdlib, and it routinely names internal detail the caller has no business
seeing: an absolute path on disk, an internal hostname and port from a
connection failure, which environment variables are unset, the exception class of
whatever library actually broke. Several of these routes are reachable before the
caller has any project scope at all.

Truncating does not help -- the leak is at the FRONT of the string.

So the detail goes to the server log under a fresh correlation id, and the caller
gets a generic sentence plus that id:

    to the caller:  "Failed to create the pull request (reference 9f2c1ab04d3e)"

    to the log:     WARNING server.routes.pr Failed to create the pull request
                    [ref=9f2c1ab04d3e]: FileNotFoundError: [Errno 2] No such file
                    or directory: '/etc/aifactory/gh.pem'
                    Traceback (most recent call last): ...

Support can still answer "what actually happened", the caller can still quote
something specific, and nothing internal crosses the boundary. This is the shape
CFactory#372 landed for the same alert class; keep the two the same.
"""

from __future__ import annotations

import logging
import secrets

from factory_common.logsafe import sanitize_log

__all__ = ["InputRejectedError", "client_error", "error_reference"]


class InputRejectedError(ValueError):
    """A validation failure whose message is DELIBERATELY safe to hand back.

    The distinction CWE-209 actually cares about is not "exception or not", it
    is *who wrote the text*. "Invalid baseBranch: must be a plain git ref" is
    developer-written and quotes only the field the caller just sent, so hiding
    it behind a correlation id turns a fixable 400 into a support ticket. A
    ``FileNotFoundError`` from the stdlib is the opposite: nobody chose its
    wording and it names a path on our disk.

    So validators raise this, :func:`client_error` hands its message back
    verbatim, and everything else still gets a reference id. Subclasses
    ``ValueError`` so existing ``except ValueError`` handlers keep working.

    The safe sentence is carried in :attr:`client_message`, a plain string this
    class was *given* at construction -- **not** read back out of the exception
    with ``str(exc)``. That distinction is the whole point of the attribute, and
    it is not cosmetic:

    ``BaseException.__str__`` renders ``args``, and ``args`` is populated by
    every exception in the process -- the stdlib's, a third-party library's, the
    runtime's. A handler that forwards ``str(exc)`` is therefore forwarding text
    of unknown authorship, and neither a reader nor a static analyser can tell
    which kind it got; that is exactly what CWE-209 is about, and it is why 104
    ``py/stack-trace-exposure`` alerts pointed at the old ``return str(exc)``
    line. ``client_message`` has exactly one writer: the constructor below,
    reached only from the eight ``raise InputRejectedError(...)`` sites in
    ``services/argv_safety.py`` and ``services/url_safety.py``. Nothing in the
    stdlib sets it, so nothing in the stdlib can reach the response through it.

    CodeQL agrees, and for a stated reason rather than by luck:
    ``StackTraceExposureQuery.qll`` adds exactly one attribute flow step, for
    ``__traceback__``, because a caught exception's *other* attributes are
    application-owned data rather than stack-trace information. See
    ``.github/codeql/VALIDATION.md``.

    What this does NOT establish is that a raise site chose its wording wisely.
    ``InputRejectedError(f"failed: {inner}")`` would launder ``inner``'s text
    straight through, and no mechanism here can stop that -- the eight messages
    are held honest by review and by ``tests/test_error_ref_client_message.py``,
    which asserts the response body of a resolver failure still carries a
    reference id rather than the resolver's text.
    """

    def __init__(self, client_message: str) -> None:
        super().__init__(client_message)
        self.client_message = client_message


#: 12 hex characters: short enough to read down a phone line, wide enough that
#: two failures in the same second do not collide.
_REF_BYTES = 6


def error_reference(
    logger: logging.Logger, context: str, exc: BaseException | str
) -> str:
    """Log ``exc`` in full under a fresh correlation id and return just the id.

    Args:
        logger: The caller's module logger, so the record lands under the module
            that actually failed.
        context: A short developer-written description of what was being
            attempted. Never interpolate request data into this.
        exc: The exception (or an already-rendered failure string).

    Returns:
        A 12-character hex id. Safe to hand to an unauthenticated caller: it
        carries no information on its own.
    """
    ref = secrets.token_hex(_REF_BYTES)
    # sanitize_log on both: an exception's text is frequently attacker-supplied
    # (a filename, a URL, a subprocess's stderr), so logging it raw would trade
    # CWE-209 for CWE-117. exc_info carries the traceback separately.
    logger.warning(
        "%s [ref=%s]: %s",
        sanitize_log(context),
        ref,
        sanitize_log(exc),
        exc_info=exc if isinstance(exc, BaseException) else None,
    )
    return ref


def client_error(logger: logging.Logger, context: str, exc: BaseException | str) -> str:
    """Return a caller-safe message: ``context`` plus a reference to the log.

    The one-liner every ``except`` handler in this server should be reaching for
    instead of ``str(e)``.
    """
    if isinstance(exc, InputRejectedError):
        # See InputRejectedError: developer-written text about the caller's own
        # input. Surfaced verbatim, and not worth a log record either -- a
        # rejected field is the validator working, not an incident.
        #
        # Read the attribute, never `str(exc)`. Same characters today; utterly
        # different provenance. Only this repo's validators can write
        # client_message, whereas str() renders whatever wrote args.
        return exc.client_message
    return f"{context} (reference {error_reference(logger, context, exc)})"
