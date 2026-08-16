import { describe, it, expect } from 'vitest';

import { toSafeUrl } from '../LivePreviewPane';
import { isHiddenDirectory } from '../task-detail/TaskFiles';

const DEFAULT_URL = 'http://localhost:3000/';

describe('toSafeUrl (js/xss-through-dom)', () => {
  it('drops script-bearing schemes that would run in this origin', () => {
    // The stored value goes straight into <iframe src> and window.open.
    expect(toSafeUrl('javascript:alert(1)')).toBe(DEFAULT_URL);
    expect(toSafeUrl('JaVaScRiPt:alert(1)')).toBe(DEFAULT_URL);
    expect(toSafeUrl('data:text/html,<img src=x onerror=alert(1)>')).toBe(DEFAULT_URL);
    expect(toSafeUrl('vbscript:msgbox(1)')).toBe(DEFAULT_URL);
    expect(toSafeUrl('file:///etc/passwd')).toBe(DEFAULT_URL);
  });

  it('never returns a value carrying the payload', () => {
    const payload = 'javascript:void(0);/*<img src=x onerror=alert(1)>*/';
    const out = toSafeUrl(payload);
    expect(out).toBe(DEFAULT_URL);
    expect(out).not.toContain('onerror');
    expect(out).not.toContain('<img');
    expect(out.startsWith('http://') || out.startsWith('https://')).toBe(true);
  });

  it('leaves genuine dev-server URLs usable', () => {
    expect(toSafeUrl('http://localhost:5173')).toBe('http://localhost:5173/');
    expect(toSafeUrl('https://example.test/app')).toBe('https://example.test/app');
  });

  it('falls back on unparseable input', () => {
    expect(toSafeUrl('')).toBe(DEFAULT_URL);
    expect(toSafeUrl('not a url')).toBe(DEFAULT_URL);
  });
});

describe('isHiddenDirectory (js/incomplete-sanitization)', () => {
  it('matches exact names and suffix rules', () => {
    expect(isHiddenDirectory('node_modules')).toBe(true);
    expect(isHiddenDirectory('.git')).toBe(true);
    expect(isHiddenDirectory('mypkg.egg-info')).toBe(true);
    expect(isHiddenDirectory('.egg-info')).toBe(true);
  });

  it('does not leak a literal star into the comparison', () => {
    // The old code did `hidden.replace('*', '')`, which strips only the first
    // occurrence, so a name still containing '*' could never match.
    expect(isHiddenDirectory('*.egg-info')).toBe(true);
    expect(isHiddenDirectory('src')).toBe(false);
    expect(isHiddenDirectory('egg-info')).toBe(false);
  });
});
