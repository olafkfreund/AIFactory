---
status: approved
issue: 1900
intent: intent/2026-10-26-1900-from-issue-repo-binding.md
---

# Spec: bind a from-issue declared repo to the project's own repository

## Design

In `routes/from_issue.py`:

- `_repo_path(value)`: normalises a repo path or git URL to a lowercase path with no leading or trailing `/`, no `.git` and no Azure `/_git/` segment. It accepts `owner/repo`, `https://host/owner/repo(.git)`, `ssh://git@host/owner/repo` and `git@host:owner/repo.git`.
- `_project_repos(project_id) -> (gitProvider | None, set[str])`: reads the project registry. The set is built from `settings.gitRepo`, `clonedFrom`, and `git -C <path> remote get-url origin`, each normalised; it is empty when none is known. The remote read is local (no network), has a timeout of a few seconds, and gives "" on any error.
- `_check_declared_repo(project_id, repo_ref)`: does nothing when nothing is declared. Otherwise it parses with `parse_repo_ref`. It raises 400 when the declared path is not in the set, or when an explicit `gitProvider` differs from the declared host.
- `create_from_issue` calls `_check_declared_repo(request.project_id, _declared_repo(request))` right after the project is resolved, before the issue fetch, the dedup lookup or any write.

## Alternatives rejected

- **Ignore `repo` and always use the project's own:** this drops the RFC-0020 host declaration, which GitLab tenants rely on, and silently does something other than what was asked.
- **Check in `_get_project_provider`:** it is shared by every GitHub route and raises `ValueError`, not an HTTP 400. The declaration comes from outside only on this route.
- **Use only `gitRepo` and `clonedFrom`:** a local-path project has neither, so the poller (which always sends `repo`) would be refused. The checkout's `origin` is the same repository, so it is included.

## Risks

- A project whose `origin` names a mirror rather than the issue's repo would now get 400. Fix it by setting `gitRepo`.
- `test_dedup_is_repo_aware` (#1775) sends two repos into one project. That test is about correlation keys, so it binds its fixture project to both repos. The production rule is unchanged.

## Verification

`tests/test_from_issue_repo_binding.py`:

- the matching repo is accepted, as `owner/repo`, in different case, or via a `clonedFrom` URL or `origin` remote;
- a foreign repo gets 400 and nothing is fetched or written;
- a declared host that contradicts an explicit `gitProvider` gets 400;
- a declared repo for a project with no known repo gets 400;
- with no repo, the fetch gets `repo_ref` `""`, which is the project's own.

`_repo_path` gets unit cases for the URL forms. `tests/test_from_issue_route.py` stays green with its fixture bound to the repos it declares.
