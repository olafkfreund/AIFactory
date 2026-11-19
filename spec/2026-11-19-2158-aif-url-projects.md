---
status: draft
issue: 2158
intent: intent/2026-11-19-2158-aif-url-projects.md
---

# Spec: URL-scoped projects in the AIFactory frontend

## Design

All under `apps/frontend-web/src`.

1. **`lib/project-route.ts` (new, pure).**
   - `slugify(name)`: lowercase, NFKD, non-`[a-z0-9]` runs to `-`, trimmed; empty falls back to the id.
   - `projectSlugs(projects)`: `Map<id, slug>`; base = `project.slug` (new optional field on `Project`) else `slugify(name)`. Backend slugs are reserved first (unique by contract). Derived slugs are then assigned oldest first (`createdAt`, then id): plain, else `-<first 8 of id>`, else that plus `-2`, `-3`... until unused, so the final set is unique. Resolution precedence: canonical slug, then id, then any alias (backend or derived slug).
   - `findProjectByRef(projects, ref)`: matches by canonical slug, derived slug, backend slug or id (case-insensitive for slugs).
   - `parseRoute(pathname)`: `/p/<ref>[/<view>]` -> `{kind:'project', ref, view}` (view defaults to `kanban`, unknown view -> `kanban`), `/projects`, else `home`. Views are the existing `SidebarView` ids.
2. **URL is authoritative (`App.tsx`).** `AuthenticatedApp` reads the route. When the URL names a project and projects are loaded, an effect makes that project the active tab and selected project (opening its tab if needed) whenever the store disagrees; `loadProjects`' restore (saved active tab, last-selected) can therefore only populate tabs, never change what the URL shows. Project views mount only when the store's active project equals the URL project, and the same step that switches the active project calls `clearTasks()` (one batched store update), so a view never renders the previous project's task array before `loadTasks` fills it. Unknown ref after load -> "Project not found" with a link to `/`. A ref that is an id or non-canonical slug is rewritten (`replace`) to the canonical slug. `activeView` is derived from the URL; `setActiveView` navigates to `/p/<slug>/<view>`. Tab click and breadcrumb select navigate; closing the URL project's tab navigates to a remaining open tab or `/`.
3. **Routes.** `/p/:slug/console/:specId` and `/p/:slug/console` render the existing console pages (they resolve the project via `findProjectByRef`); `/console/:projectId[/:specId]` render a redirect to the `/p/...` form once projects are loaded. These routes bypass `AuthenticatedApp`, so the console pages and the redirect call a small `useEnsureProjects()` hook (calls `loadProjects()` when the store is empty and not loading) so a cold browser load resolves. `/projects` = Projects page (list, open, add via the existing `AddProjectModal`, delete with typed-name confirm calling the existing `removeProject`: unregister only). `/` = the existing `WelcomeScreen` fed from the store as the "My projects" page (links navigate to `/p/<slug>`); no auto-redirect.
4. **Login return.** The `/login` redirect for an unauthenticated visitor stores `pathname+search` in `sessionStorage` (`aif-return-to`); after auth, the app consumes it once at `/` and navigates there. Covers password and OIDC login.
5. **Top bar.** `ProjectTabBar` swaps `ProjectSelector` and the "+" button for the vendored `ProjectBreadcrumb` (`components/ProjectBreadcrumb.tsx`, byte-copy of Factory#3858) with an "All projects" row to `/projects`. `ProjectSelector.tsx` is deleted (its only user). Cmd-K gains one command "Switch project: <name>" per project (existing `commands` list; the hub palette's own Switch-project command is adopted when the pin is bumped).
6. **Drift workflow.** Add `ProjectBreadcrumb.tsx` to the `paths` filter of `.github/workflows/factory-ui-drift.yml` (and its header comment) so later breadcrumb-only PRs are compared.
7. **Hub pin.** Pin bump needs the Factory change on hub main (`assert-hub-pin-on-main.sh`), so this PR vendors only the new `ProjectBreadcrumb.tsx` and leaves `PortalSwitcher.tsx`/`CommandPalette.tsx` and `HUB_PIN_SHA` untouched; re-vendoring those two plus the pin bump is a follow-up PR once Factory#3858 is merged (the pin must be on hub main, so hub-first is enforced by CI; vendoring the one new file early is harmless because the checker tolerates it until pinned, see Factory spec item 4). `PortalSwitcher` therefore does not carry the slug yet.

## Alternatives rejected

- Keep views as state and only mirror the URL: two sources of truth; the bug being fixed.
- Redirect `/` to the last project: hidden state, and a first-time user would not see add/list.
- Make `/p/:slug` separate Route elements per view: remounts the long-lived terminal grid on every view change; one `/*` route that parses keeps it mounted.
- Change `loadProjects`: widens the diff and the store tests; the URL effect achieves precedence.

## Risks

- Brief store/URL disagreement while `loadProjects` finishes: views are gated until they agree.
- Slug collisions for name-derived slugs: deterministic rule above; backend slugs (phase 1) replace them and old derived slugs keep resolving.
- Tab close/select paths must navigate or the URL effect will reopen the closed tab: covered by tests.
- Drift gate is not exercised for the new vendored file until the pin bump.

## Verification

Vitest: `project-route` unit tests (slugify, collisions, ref resolution, parseRoute); a routing test with `MemoryRouter` + the real store (including: switching between projects with distinct tasks never renders the previous project's tasks): URL beats saved active tab, id ref rewritten to slug, unknown slug -> not found, back/forward re-syncs, two stores/tabs on two URLs show their own project; legacy console redirect; login return; typed-name delete confirm; `ProjectBreadcrumb` component test. Existing suites, `npm run typecheck`, `npm run lint`.
