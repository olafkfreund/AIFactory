---
status: draft
issue: 2158
author: olafkfreund
---

# Intent: URL-scoped projects in the AIFactory frontend

## Problem

The current project lives in zustand, localStorage and a server `tab-state.json`, not in the URL. Every view is gated by `selectedProject`, only `/console/:projectId` carries an id, links are not shareable, two browser tabs cannot show two projects reliably, and the header `ProjectSelector` unregisters a project with one click and no confirmation (epic Factory#3857, phase 4).

## Proposed outcome

- Every project view lives under `/p/:slug/...`; the URL decides the project. Reload, back/forward and two tabs on two projects all behave. Saved tab state restores open tabs only and never overrides the URL.
- `/` is a "My projects" page. `/projects` is a Projects page holding add and delete (typed-name confirm; delete still only unregisters).
- The top bar shows the shared `ProjectBreadcrumb` instead of `ProjectSelector`; Cmd-K has "Switch project...".
- Old routes (`/console/:projectId/...`) redirect to `/p/:slug/console/...`.
- Slug = `project.slug` when the backend sends it, else a deterministic slugify(name) (id fallback when empty); the oldest project keeps the plain slug on a name collision so existing links stay stable, newer ones get an id suffix; an id or any derived/backend slug in the URL also resolves and is rewritten to the canonical slug.
- A deep link opened while logged out returns to the same URL after login. Works before and after backend phase 1.

## Affected users and systems

AIFactory web users; `apps/frontend-web` only. Shared files vendored from Factory#3858 (hub pin bump in the drift workflow).

## Constraints

- Frontend only; no backend change.
- Vendored shared files stay byte-identical to the Factory hub copy.
- Existing vitest suites stay green; new tests fail without the change.
- Delete semantics unchanged (unregister only).

## Open questions

None. Decision (owner delegation 2026-11-19): no auto-redirect from `/` (simplest and least surprising); the breadcrumb and the project tabs are the quick path back into a project.
