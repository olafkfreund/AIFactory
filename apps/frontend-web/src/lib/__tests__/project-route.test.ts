import { beforeEach, describe, expect, it } from 'vitest';
import { findProjectByRef, parseRoute, projectPath, projectSlug, projectSlugs, slugify } from '../project-route';
import type { Project } from '../../shared/types';

const project = (id: string, name: string, createdAt: string, slug?: string): Project =>
  ({ id, name, slug, createdAt: new Date(createdAt) }) as Project;

describe('project routes', () => {
  beforeEach(() => localStorage.clear());
  it('normalizes names and falls back to the id', () => {
    expect(slugify('My  Project_1!', 'x')).toBe('my-project-1');
    expect(slugify('Café', 'x')).toBe('cafe');
    expect(slugify('日本', 'abc123')).toBe('abc123');
  });

  it('gives every collision member a case-insensitive id slug and reserves the bare name', () => {
    const older = project('aaaaaaaa-1', 'Demo', '2026-01-01');
    const newer = project('bbbbbbbb-2', 'Demo', '2026-02-01');
    expect(projectSlugs([newer, older]).get(older.id)).toBe('demo-aaaaaaaa-1');
    expect(projectSlug([newer, older], newer)).toBe('demo-bbbbbbbb-2');
    expect(findProjectByRef([older, newer], 'DEMO')).toBeUndefined();
    const reserved = project('c', 'Other', '2026-03-01', 'DEMO');
    expect(projectSlugs([older, reserved]).get(older.id)).toBe('demo-aaaaaaaa-1');
    expect([...projectSlugs([reserved, older])]).toEqual([...projectSlugs([older, reserved])]);
    expect(findProjectByRef([reserved, older], 'demo')).toBeUndefined();
    const collision = project('d', 'Demo bbbbbbbb', '2026-03-01');
    expect(new Set(projectSlugs([older, newer, collision]).values()).size).toBe(3);
  });

  it('always accepts IDs before slugs and never hands an issued slug to another project', () => {
    const canonical = project('a', 'Alpha', '2026-01-01', 'shared');
    const idMatch = project('shared', 'Beta', '2026-01-02');
    const alias = project('c', 'Shared', '2026-01-03', 'other');
    expect(findProjectByRef([alias, idMatch, canonical], 'SHARED')).toBe(idMatch);
    expect(findProjectByRef([alias, idMatch], 'shared')).toBe(idMatch);
    expect(findProjectByRef([alias], 'shared')).toBeUndefined();
    expect(findProjectByRef([canonical], 'A')).toBe(canonical);
    expect(findProjectByRef([canonical], 'missing')).toBeUndefined();
  });

  it('keeps a collision alias after the older twin is removed', () => {
    const older = project('aaaaaaaa-1', 'Demo', '2026-01-01');
    const newer = project('bbbbbbbb-2', 'Demo', '2026-02-01');
    expect(findProjectByRef([older, newer], 'demo-bbbbbbbb-2')).toBe(newer);
    expect(findProjectByRef([newer], 'demo-bbbbbbbb-2')).toBe(newer);
    expect(findProjectByRef([newer], 'demo')).toBeUndefined();
  });

  it('parses routes, defaults unknown views, and round-trips encoded references', () => {
    expect(parseRoute('/p/alpha')).toEqual({ kind: 'project', ref: 'alpha', view: 'kanban' });
    expect(parseRoute('/p/alpha/terminals')).toEqual({ kind: 'project', ref: 'alpha', view: 'terminals' });
    expect(parseRoute('/p/alpha/settings')).toEqual({ kind: 'project', ref: 'alpha', view: 'settings' });
    expect(parseRoute('/p/alpha/bogus')).toEqual({ kind: 'project', ref: 'alpha', view: 'kanban' });
    expect(parseRoute('/p/%E0%A4%A')).toMatchObject({ kind: 'project' });
    expect(parseRoute(projectPath('a b', 'terminals'))).toEqual({ kind: 'project', ref: 'a b', view: 'terminals' });
    expect(projectPath('x', 'kanban')).toBe('/p/x');
    expect(parseRoute('/projects/')).toEqual({ kind: 'projects' });
    expect(parseRoute('/preferences')).toEqual({ kind: 'preferences' });
    expect(parseRoute('/')).toEqual({ kind: 'home' });
  });
});
