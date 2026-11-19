---
status: approved
issue: 2158
spec: spec/2026-11-19-2158-aif-url-projects.md
coders:
  4: codex   # self-contained new page (typed-name confirm), easy to review in isolation
reviewers:
  - codex    # read-only review of each draft and of the final diff (agy is off by owner decision)
---

# Plan: URL-scoped projects in the AIFactory frontend

Approved spec decisions (carried over): URL is authoritative via `/p/:slug[/:view]`; pure `lib/project-route.ts` (slugify, unique `projectSlugs`, `findProjectByRef` with canonical > id > alias precedence, `parseRoute`); an effect in `AuthenticatedApp` syncs store to URL, clears tasks in the same step, gates views until active project equals URL project, rewrites id/alias refs to the canonical slug; `/projects` page (add via `AddProjectModal`, delete with typed-name confirm = existing unregister); `/` = existing `WelcomeScreen`, no auto-redirect; console under `/p/:slug/console[/:specId]`, legacy `/console/...` redirects, `useEnsureProjects()` for routes outside `AuthenticatedApp`; login return via `sessionStorage` key `aif-return-to`; `ProjectBreadcrumb` (vendored byte-copy from Factory#3858) replaces `ProjectSelector` + "+" in `ProjectTabBar`; Cmd-K per-project "Switch project: <name>" commands; drift workflow path filter gains `ProjectBreadcrumb.tsx`; `HUB_PIN_SHA`, `PortalSwitcher.tsx`, `CommandPalette.tsx` untouched (follow-up after Factory merge). Frontend only.

All paths below are under `apps/frontend-web/`.

## Steps

1. `src/lib/project-route.ts` (new) + `src/lib/__tests__/project-route.test.ts`; add `slug?: string` to `Project` in `src/shared/types/project.ts`. → verify by `npx vitest run src/lib`.
   Traps: ruff does not apply; eslint `--max-warnings` ratchet (run `npm run lint`).
2. `src/components/ProjectBreadcrumb.tsx` = byte copy of the Factory hub file at the final head of Factory#3858 (never edited locally); `src/components/__tests__/ProjectBreadcrumb.test.tsx`; add to eslint ignores / format exclusions wherever `PortalSwitcher.tsx` is excluded (grep for it). → verify by `cmp` against the hub file and vitest.
3. `src/App.tsx`: URL routing per spec items 2-3: route parse, sync effect (`openProjectTab`, `selectProject`, `clearTasks`), view gating, not-found state, canonical rewrite, `setActiveView` navigates, tab select/close navigate, `/` and `/projects` branches, Cmd-K switch commands, routes for `/p/:slug/console...` and legacy `/console/...` redirect, login return, `src/hooks/useEnsureProjects.ts`; adjust `pages/ConsolePage.tsx` and `pages/ConsoleGridPage.tsx` to resolve `projectId` or `slug` via `findProjectByRef` and link to `/p/<slug>/console...`. Tests: `src/__tests__/project-routing.test.tsx` (MemoryRouter + real store: URL beats saved tab, id rewritten, unknown slug, back/forward, two projects' tasks never mixed, legacy redirect, login return). → verify by `npx vitest run src/__tests__/project-routing.test.tsx`.
   Also `src/stores/task-store.ts` `loadTasks`: a module-level request counter; a response from a superseded request is dropped (test: A resolves after B, B's tasks stay).
   Terminal lifetime: `TerminalGrid` is rendered whenever the route is `/p/<known project>` (its props come from the URL project), outside the store-equality gate that guards the other views, so a project switch does not unmount it; it unmounts on `/` and `/projects` as it does today when no project is selected (regression test counts mounts across a switch).
   Traps: keep `/*` as one route; do not edit `loadProjects`.
4. (codex) `src/pages/ProjectsPage.tsx` (new): list of all projects (name, path, Open link to `/p/<slug>`), "Add project" opening `AddProjectModal`, per-row Delete opening a dialog (existing `ui/dialog`) that enables the confirm button only when the typed text equals the project name, then calls `removeProject(id)` from `stores/project-store.ts`. Props: `projects`, `onAdd`, `onOpen(project)`. Test `src/pages/__tests__/ProjectsPage.test.tsx` (confirm disabled until exact name; calls removeProject once). → verify by `npx vitest run src/pages`.
   Traps: delete wording says "unregister" (files untouched); no trash icon one-click anywhere.
5. `src/components/settings/AppSettings.tsx` (the only other `ProjectSelector` caller, lines 44 and 297-300, handler 162-164): replace it with a read-only label of the URL project's name (Settings edits the project the URL shows; no store-only selection that the URL effect would revert) and drop `handleProjectChange`. `src/components/ProjectTabBar.tsx`: replace `ProjectSelector` and the "+" button with `ProjectBreadcrumb` (props from `projects` store + `onSelect`/`onViewAll` callbacks from `App`); delete `src/components/settings/ProjectSelector.tsx` and fix its imports/exports (grep shows only `ProjectTabBar.tsx` and `AppSettings.tsx`); update `ProjectTabBar.test.tsx` if it references them. → verify by `npx vitest run` (full), `npm run typecheck`, `npm run lint`.
6. `.github/workflows/factory-ui-drift.yml` (repo root): add `ProjectBreadcrumb.tsx` to `paths` and header comment. → verify by `python -c "import yaml;yaml.safe_load(open('.github/workflows/factory-ui-drift.yml'))"`.

## Tests

`npx vitest run`, `npm run typecheck`, `npm run lint` in `apps/frontend-web`; new routing/route-lib/breadcrumb/projects-page tests fail without the change.

## Rollback

Revert the PR; the backend and stored `projects.json`/tab state are untouched, so old routes and the dropdown return.

## Deviations (recorded with the code)

- Step 4: `ProjectsPage` text and the project-not-found message use i18n keys `welcome:projectsPage.*` (en, fr, pt-BR) per AGENTS.md; the page is wrapped in a scrolling container (final-review findings). The breadcrumb copy was re-vendored after hub combobox/aria-activedescendant fixes.
- Step 3: `/p/:slug/console` pages and the legacy redirect are in `App.tsx`; the "Copy console link" in `TaskMetadata.tsx` still emits the legacy URL, which redirects (left out to keep the diff narrow).
