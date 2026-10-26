---
status: approved
issue: 1900
author: olafkfreund
---

# Intent: bind a from-issue declared repo to the project's own repository

## Problem

`POST /api/tasks/from-issue` takes an optional `repo` (`routes/from_issue.py:67`). `_get_project_provider` (`routes/github.py:976`, lines 1028-1032) lets that declared repo outrank the project's `gitRepo` but keeps the project's `gitToken`. So a member of project A can point A's credential at any repository: the issue fetch, the spec's `githubIssue.repo` and the PR endgame all follow the declared repo. #1898 checks membership of A. Nothing yet stops A's membership from reaching repos A does not own.

## Proposed outcome

- A declared `repo` must name the project's own repository, or the route answers 400 before anything is fetched or written.
- A request without `repo` behaves as today and uses the project's own repo.
- Live intake keeps working. `AIFACTORY_INTAKE_REPOS` (factory-gitops) maps one repo per project: `aifactory-demo` → its project and `TFactory` → its project.

## Affected users and systems

AIFactory web-server from-issue route; the intake poller (sends `repo` = its configured repo, which matches); external callers declaring `repo`.

## Constraints

- The project's own repository is what the project already records: `settings.gitRepo`, the `clonedFrom` URL, or its checkout's `origin` remote. No new setting.
- If the project has an explicit `gitProvider`, a declared host that contradicts it is rejected too. An unset `gitProvider` (default `github`) keeps the RFC-0020 rule that a declaration may name the host.
- Fail closed: a declared repo for a project with no known repository is rejected.
- No credential, gitops or cluster change.

## Open questions

None.
