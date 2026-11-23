// URL <-> project mapping for /p/:slug[/:view].
import type { Project } from '../shared/types';

export const PROJECT_VIEWS = [
  'kanban', 'terminals', 'editor', 'context', 'github-issues', 'github-prs',
  'changelog', 'insights', 'worktrees', 'agent-tools', 'skills', 'settings',
] as const;
export type ProjectView = (typeof PROJECT_VIEWS)[number];

export type AppRoute =
  | { kind: 'project'; ref: string; view: ProjectView }
  | { kind: 'projects' }
  | { kind: 'preferences' }
  | { kind: 'home' };

export function slugify(name: string, fallback: string): string {
  const slug = name.normalize('NFKD').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return slug || fallback;
}

// shortcut: browser-local slug ownership, server-side reservation comes with the #3857 phase 3 registry
const RESERVATIONS = 'aifactory-project-slug-reservations';
type Reservations = Record<string, string | null>;

function reservations(): Reservations {
  try { return JSON.parse(localStorage.getItem(RESERVATIONS) || '{}') as Reservations; }
  catch { return {}; }
}

const baseSlug = (project: Project) => (project.slug || slugify(project.name, project.id)).toLowerCase();

export function projectsForBareSlug(projects: Project[], ref: string): Project[] {
  return projects.filter((project) => baseSlug(project) === ref.toLowerCase());
}

export function isBareSlugChooser(projects: Project[], ref: string): boolean {
  const group = projectsForBareSlug(projects, ref);
  return group.length > 1 || (group.length > 0 && reservations()[ref.toLowerCase()] === null);
}

export function projectSlugs(projects: Project[]): Map<string, string> {
  const slugs = new Map<string, string>();
  const history = reservations();
  const groups = new Map<string, Project[]>();
  for (const project of projects) {
    const base = baseSlug(project);
    groups.set(base, [...(groups.get(base) || []), project]);
  }
  const used = new Set(projects.map((project) => project.id.toLowerCase()));
  for (const [base, group] of groups) {
    if (group.length > 1) history[base] = null;
    else if (!(base in history)) history[base] = group[0].id;
  }
  for (const project of [...projects].sort((a, b) => a.id.localeCompare(b.id))) {
    const base = baseSlug(project);
    const withId = `${base}-${project.id.toLowerCase()}`;
    let slug = groups.get(base)?.length === 1 && history[base] === project.id && !used.has(base)
      ? base : withId;
    for (let n = 2; used.has(slug) || (slug in history && history[slug] !== project.id); n++) slug = `${withId}-${n}`;
    used.add(slug);
    slugs.set(project.id, slug);
    if (!(slug in history)) history[slug] = project.id;
  }
  try { localStorage.setItem(RESERVATIONS, JSON.stringify(history)); } catch { /* Storage may be unavailable. */ }
  return slugs;
}

export const projectSlug = (projects: Project[], project: Project): string =>
  projectSlugs(projects).get(project.id) ?? project.id;

export function findProjectByRef(projects: Project[], ref: string): Project | undefined {
  const normalized = ref.toLowerCase();
  const byId = projects.find((project) => project.id.toLowerCase() === normalized);
  if (byId) return byId;
  if (isBareSlugChooser(projects, normalized)) return undefined;
  const slugs = projectSlugs(projects);
  const legacyMatches = projects.filter((project) => `${baseSlug(project)}-${project.id.slice(0, 8)}`.toLowerCase() === normalized);
  return projects.find((project) => slugs.get(project.id)?.toLowerCase() === normalized)
    ?? projects.find((project) => reservations()[normalized] === project.id)
    ?? (legacyMatches.length === 1 ? legacyMatches[0] : undefined);
}

export function parseRoute(pathname: string): AppRoute {
  const match = /^\/p\/([^/]+)(?:\/([^/]+))?/.exec(pathname);
  if (match) {
    const view = PROJECT_VIEWS.find((candidate) => candidate === match[2]) ?? 'kanban';
    let ref = match[1];
    try { ref = decodeURIComponent(ref); } catch { /* Keep malformed references for not-found. */ }
    return { kind: 'project', ref, view };
  }
  if (/^\/projects\/?$/.test(pathname)) return { kind: 'projects' };
  if (/^\/preferences\/?$/.test(pathname)) return { kind: 'preferences' };
  return { kind: 'home' };
}

export const projectPath = (slug: string, view?: ProjectView | string): string =>
  `/p/${encodeURIComponent(slug)}${view && view !== 'kanban' ? `/${view}` : ''}`;
