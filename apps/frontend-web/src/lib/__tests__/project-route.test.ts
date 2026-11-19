import { describe, it, expect } from 'vitest';
import { findProjectByRef, parseRoute, projectPath, projectSlugs, slugify } from '../project-route';
import type { Project } from '../../shared/types';

const proj = (id: string, name: string, createdAt: string, slug?: string): Project =>
  ({ id, name, slug, path: `/p/${id}`, createdAt: new Date(createdAt) }) as unknown as Project;

describe('slugify', () => {
  it('normalizes and falls back to the id', () => {
    expect(slugify('My  Project_1!', 'x')).toBe('my-project-1');
    expect(slugify('Café', 'x')).toBe('cafe');
    expect(slugify('日本', 'abc123')).toBe('abc123');
  });
});

describe('projectSlugs', () => {
  it('oldest keeps the plain slug; newcomers cannot steal it', () => {
    const a = proj('aaaaaaaa-1', 'Demo', '2026-01-01');
    const b = proj('bbbbbbbb-2', 'Demo', '2026-02-01');
    expect(projectSlugs([b, a]).get(a.id)).toBe('demo');
    expect(projectSlugs([b, a]).get(b.id)).toBe('demo-bbbbbbbb');
  });
  it('final slugs are unique even for adversarial names', () => {
    const ps = [
      proj('aaaaaaaa-1', 'Demo', '2026-01-01'),
      proj('bbbbbbbb-2', 'Demo', '2026-02-01'),
      proj('cccccccc-3', 'Demo bbbbbbbb', '2026-03-01'),
    ];
    const slugs = [...projectSlugs(ps).values()];
    expect(new Set(slugs).size).toBe(3);
  });
  it('backend slugs win and are reserved first', () => {
    const a = proj('a1', 'Demo', '2026-01-01');
    const b = proj('b2', 'Other', '2026-02-01', 'demo');
    expect(projectSlugs([a, b]).get('b2')).toBe('demo');
    expect(projectSlugs([a, b]).get('a1')).not.toBe('demo');
  });
});

describe('findProjectByRef', () => {
  const a = proj('id-a', 'Alpha App', '2026-01-01');
  const b = proj('id-b', 'Beta', '2026-01-02', 'beta-x');
  it('resolves canonical slug, id and alias', () => {
    expect(findProjectByRef([a, b], 'alpha-app')?.id).toBe('id-a');
    expect(findProjectByRef([a, b], 'ID-A')?.id).toBe('id-a');
    expect(findProjectByRef([a, b], 'beta-x')?.id).toBe('id-b');
    expect(findProjectByRef([a, b], 'beta')?.id).toBe('id-b'); // derived alias
    expect(findProjectByRef([a, b], 'nope')).toBeUndefined();
  });
});

describe('parseRoute', () => {
  it('parses project routes and defaults the view', () => {
    expect(parseRoute('/p/alpha')).toEqual({ kind: 'project', ref: 'alpha', view: 'kanban' });
    expect(parseRoute('/p/alpha/terminals')).toEqual({ kind: 'project', ref: 'alpha', view: 'terminals' });
    expect(parseRoute('/p/alpha/bogus')).toEqual({ kind: 'project', ref: 'alpha', view: 'kanban' });
    expect(parseRoute('/p/a%20b')).toMatchObject({ ref: 'a b' });
    expect(parseRoute('/p/%E0%A4%A')).toMatchObject({ kind: 'project' });
  });
  it('parses the other routes', () => {
    expect(parseRoute('/projects')).toEqual({ kind: 'projects' });
    expect(parseRoute('/')).toEqual({ kind: 'home' });
    expect(parseRoute('/whatever')).toEqual({ kind: 'home' });
  });
  it('projectPath round-trips', () => {
    expect(projectPath('a b', 'terminals')).toBe('/p/a%20b/terminals');
    expect(projectPath('x')).toBe('/p/x');
  });
});
