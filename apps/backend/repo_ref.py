"""The task contract's provider-qualified repo reference (RFC-0020 3.5, Factory#366).

**The bug this closes.** A GitLab tenant's PARR run opened a GitHub pull
request. AIFactory chose its git host from its own configuration — the project's
``gitProvider`` setting, defaulting to ``github`` — and the PR endgame did not
consult a provider at all, it shelled out to ``gh``. So the choice a tenant made
in CFactory's Settings panel had no effect on where the work actually landed.

The contract already carried a repo reference. Since phase 5 that reference may
be **provider-qualified**::

    owner/repo                              -> ("github", "owner/repo")
    gitlab:group/subgroup/project           -> ("gitlab", "group/subgroup/project")
    azure_devops:org/project/repo           -> ("azure_devops", "org/project/repo")

Three rules, and they are the whole contract:

1. **GitHub is the unqualified default.** An unqualified reference reads as
   ``github``. Every pre-phase-5 contract and every GitHub contract is unchanged,
   and nothing has to be backfilled — which is what makes this safe to deploy to
   a live fleet.
2. **Only a KNOWN provider is a qualification.** ``https://gitlab.example/g/p``
   is a clone URL, not a project on a host called ``https``. Splitting on the
   first colon regardless is how a "generic" parser breaks the one caller that
   had been working.
3. **There is no sibling ``provider`` field to reconcile.** Where an older field
   exists (``FromIssueRequest.provider``, ``RepoConfig.provider``) the
   QUALIFICATION WINS, and the old field is the fallback for a reference that
   carries none. One answer, and a stated precedence for the transition.

Deliberately not in ``runners/github/`` — that tree is a byte-for-byte vendored
copy of the hub's canonical provider layer, guarded by a drift gate. This is
contract-reading code, not a VCS client, and putting it there would mean a hub
change plus four re-vendors plus four pinned-SHA bumps to ship a twelve-line
parser.
"""

from __future__ import annotations

# The providers this fleet actually implements. Bitbucket and Gitea are declared
# in the canonical ProviderType and unimplemented, so treating one as a
# qualification would route work to a client that only ever raises.
GITHUB = "github"
GITLAB = "gitlab"
AZURE_DEVOPS = "azure_devops"
SUPPORTED_PROVIDERS: tuple[str, ...] = (GITHUB, GITLAB, AZURE_DEVOPS)


def parse_repo_ref(ref: str | None) -> tuple[str, str] | None:
    """``(provider, project)`` for a repo reference, or ``None`` if there is none."""
    value = (ref or "").strip()
    if not value:
        return None
    head, sep, tail = value.partition(":")
    if sep and head.strip().lower() in SUPPORTED_PROVIDERS and tail.strip():
        return head.strip().lower(), tail.strip()
    return GITHUB, value


def provider_of(ref: str | None, default: str = GITHUB) -> str:
    """Which host ``ref`` names. ``default`` covers a reference that is absent.

    A reference that is present but unqualified is GitHub — not ``default``.
    Saying otherwise would let an env var override an explicit declaration,
    which is the exact failure this module exists to remove.
    """
    parsed = parse_repo_ref(ref)
    return parsed[0] if parsed else (default or GITHUB).strip().lower()


def project_of(ref: str | None) -> str:
    """The bare project path, with any qualification stripped.

    What ``gh``, a clone URL or a provider's ``repo=`` argument needs: they all
    take ``owner/repo``, never ``gitlab:owner/repo``.
    """
    parsed = parse_repo_ref(ref)
    return parsed[1] if parsed else ""


def qualify_repo(provider: str | None, project: str | None) -> str:
    """``project`` tagged with its host — the inverse of :func:`parse_repo_ref`."""
    if not project:
        return ""
    kind = (provider or GITHUB).strip().lower()
    return project if kind == GITHUB else f"{kind}:{project}"


def is_github(ref: str | None, default: str = GITHUB) -> bool:
    """True when ``ref`` names GitHub, so a ``gh``-CLI path may run.

    The guard on every GitHub-shaped path. Phrased positively on purpose: a new
    caller has to opt IN to the ``gh`` CLI, rather than remembering to exclude
    two hosts and silently running against a third that gets added later.
    """
    return provider_of(ref, default) == GITHUB
