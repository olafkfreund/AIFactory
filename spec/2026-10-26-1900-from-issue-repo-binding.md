---
status: approved
issue: 1900
intent: intent/2026-10-26-1900-from-issue-repo-binding.md
---

# Spec: bind a from-issue declared repo to the project's own repository

## Design

Revised after the review of #1901: identity is (host, path), not path alone.

In `routes/from_issue.py`:

- `_repo_id(value, base) -> (host, path)`: `host` is "" for a bare `owner/repo`. Accepts `https://`, `ssh://` and scp-like `git@host:path` URLs. The path is lowercased, without `.git` or edge slashes. Azure forms collapse to `dev.azure.com` + `org/proj/repo`: `/_git/` is dropped, `git@ssh.dev.azure.com:v3/...` (and `vs-ssh.visualstudio.com`) lose `v3/`, and `https://org.visualstudio.com/proj/_git/repo` becomes `org/proj/repo`. `base` is the project's `gitBaseUrl` as (host, subpath). A URL on that host loses the subpath, which covers self-hosted GitLab under `/gitlab`.
- `_project_repos(project_id, path) -> _OwnRepo(provider, paths, hosts, base)`:
  - It reads `settings.gitRepo`, `clonedFrom` and the checkout's `origin` (via `routes/git.run_git_command`, local only).
  - `hosts` are their hosts plus the `gitBaseUrl` host.
  - `provider` is `gitProvider` when set. Otherwise it comes from the first own host with a known provider (`github.com`, `gitlab.com`, `dev.azure.com`). With no host at all it is `github`, which is what `_get_project_provider` defaults to; that provider's public host then becomes the only own host. A self-hosted host with `gitProvider` unset derives no provider.
- `_check_declared_repo` returns 400 unless all three of these hold:
  - the declared path is one of the own paths;
  - a declared host, if any, is one of the own hosts (a bare `owner/repo` is compared by path only);
  - the declared provider prefix equals the project's provider, when that is known.
  An unqualified URL is `github:` (RFC-0020 `parse_repo_ref`), so a GitLab project is declared as `gitlab:<url>` or with the `provider` hint.
- `create_from_issue` runs the check right after the project is resolved, before the fetch, the dedup lookup or any write.

## Alternatives rejected

- **Ignore `repo` and always use the project's own:** this drops the RFC-0020 host declaration, which GitLab tenants rely on, and silently does something other than what was asked.
- **Check in `_get_project_provider`:** it is shared by every GitHub route and raises `ValueError`, not an HTTP 400. The declaration comes from outside only on this route.
- **Compare paths only** (first revision): it let `https://evil.example/owner/repo` and the other hosts' forms through, and a `gitlab:` prefix through for a GitHub project. Rejected in the review of #1901.
- **Use only `gitRepo` and `clonedFrom`:** a local-path project has neither, so the poller (which always sends `repo`) would be refused. The checkout's `origin` is the same repository, so it is included.

## Risks

- A project whose `origin` names a mirror rather than the issue's repo would now get 400. Fix it by setting `gitRepo`.
- `test_dedup_is_repo_aware` (#1775) sends two repos into one project. That test is about correlation keys, so it binds its fixture project to both repos. The production rule is unchanged.

## Verification

`tests/test_from_issue_repo_binding.py`:

- The project's own repo is accepted as a bare path, a `github:` path, `.git`, an https URL and an scp URL. `gitRepo` and `clonedFrom` count as own.
- These get 400 with nothing fetched or written: a foreign path; `https://evil.example/owner/repo`; `git@evil.example:owner/repo`; `github:https://evil.example/owner/repo`; and `gitlab:` or `azure_devops:` for a GitHub project with `gitProvider` unset. A host that contradicts an explicit `gitProvider` also gets 400.
- The payload path is bound too. No repo falls back to the project's own. A project with no known repo rejects a declaration, with the fetch mocked so no network is reached.
- Azure: the clone forms `git@ssh.dev.azure.com:v3/...`, `org.visualstudio.com/.../_git/...` and `dev.azure.com/.../_git/...` all accept `azure_devops:org/proj/repo`, and the provider derived from the host rejects `github:`.
- Self-hosted GitLab under a subpath: `gitlab:group/proj` and the qualified URL are accepted. Another path, `gitlab.com` with the same path, and an unqualified URL are rejected.
- Unit cases for `_repo_id` cover the URL forms and the base-path strip.

`tests/test_from_issue_route.py` stays green, with its fixture bound to the repos it declares.
