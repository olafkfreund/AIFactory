// Run with: npm run test:scripts
//
// Covers the two security fixes in bump-version.js:
//   - js/file-system-race, and the ENOENT-vs-unreadable distinction that the
//     existsSync-then-read shape silently erased (readIfPresent)
//   - js/regex-injection + js/incomplete-sanitization (changelogHasVersion)
//   - setOpenapiVersion (#1631): info:-scoped constant regex, replacer fn (no $-expansion)
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { readIfPresent, changelogHasVersion, setOpenapiVersion } = require('./bump-version.js');

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'bumpver-'));

test('readIfPresent returns contents for a file that exists', () => {
  const file = path.join(tmp(), 'here.txt');
  fs.writeFileSync(file, 'contents');
  assert.equal(readIfPresent(file), 'contents');
});

test('readIfPresent returns null for a genuinely missing file', () => {
  assert.equal(readIfPresent(path.join(tmp(), 'nope.txt')), null);
});

test('readIfPresent rethrows a non-ENOENT error instead of reporting "absent"', () => {
  // Reading a directory raises EISDIR, not ENOENT. The old
  // `existsSync(p) && readFileSync(p)` shape collapsed every such failure into
  // "missing", so the bump skipped the file and reported success.
  assert.throws(
    () => readIfPresent(tmp()),
    (err) => err.code !== undefined && err.code !== 'ENOENT'
  );
});

const CHANGELOG = '# Changelog\n\n## 9.9.9 - 2026-08-13\n\n- thing\n\n## 9.9.8\n\n- older\n';

test('changelogHasVersion finds a real header', () => {
  assert.equal(changelogHasVersion(CHANGELOG, '9.9.9'), true);
  assert.equal(changelogHasVersion(CHANGELOG, '9.9.8'), true);
  assert.equal(changelogHasVersion(CHANGELOG, '9.9.7'), false);
});

test('changelogHasVersion does not match a version prefix', () => {
  // "9.9" must not satisfy "## 9.9.9" -- the next char is '.', not \s or '-'.
  assert.equal(changelogHasVersion(CHANGELOG, '9.9'), false);
});

test('regex metacharacters in the version are matched literally', () => {
  assert.equal(changelogHasVersion(CHANGELOG, '.*'), false);
  assert.equal(changelogHasVersion(CHANGELOG, '[0-9].[0-9].[0-9]'), false);
  assert.equal(changelogHasVersion(CHANGELOG, '\\S+'), false);

  // Sanity: the construction this replaced really was injectable. It escaped
  // dots and left every other metacharacter live, so these two "versions"
  // matched a changelog that contains neither of them literally.
  //
  // The escaped forms are written out as literals rather than produced by the
  // old `version.replace(/\./g, '\\.')` call, so this test does not itself
  // carry the incomplete sanitizer it is describing.
  const legacy = (escapedVersion) =>
    new RegExp(`^## ${escapedVersion}(\\s|-)`, 'm').test(CHANGELOG);
  assert.equal(legacy('\\S+'), true);
  assert.equal(legacy('[0-9]\\.[0-9]\\.[0-9]'), true);
});

// apps/web-server/openapi.yaml:1-6 verbatim, em dash included.
const OPENAPI_HEADER =
  'openapi: 3.1.0\ninfo:\n  title: AIFactory Web API\n' +
  '  description: Web API for AIFactory — self-hosted AI task management + agent orchestration\n' +
  '  version: 3.6.84\npaths:\n';

// Every line but `onlyIdx` is byte-identical; line `onlyIdx` equals `want`.
const assertOnlyLineChanged = (before, after, onlyIdx, want) => {
  const a = before.split('\n');
  const b = after.split('\n');
  assert.equal(b.length, a.length);
  b.forEach((line, i) => assert.equal(line, i === onlyIdx ? want : a[i], `line ${i + 1}`));
};

test('setOpenapiVersion rewrites only info.version in the real header', () => {
  assertOnlyLineChanged(OPENAPI_HEADER, setOpenapiVersion(OPENAPI_HEADER, '9.99.0'), 4, '  version: 9.99.0');
});

test('setOpenapiVersion leaves a description mention and a nested version alone', () => {
  const spec =
    'openapi: 3.1.0\ninfo:\n  title: T\n' +
    '  description: pinned to version: 1 of the wire format\n' +
    '  version: 1.2.3\npaths: {}\ncomponents:\n  schemas:\n    Thing:\n      properties:\n' +
    '        version: 7.7.7\n';
  assertOnlyLineChanged(spec, setOpenapiVersion(spec, '9.99.0'), 4, '  version: 9.99.0');
});

test('setOpenapiVersion returns null when info has no version', () => {
  // The 2-space `version:` under components is load-bearing: an info-unscoped
  // regex would match it and return a string instead of null.
  const spec = 'openapi: 3.1.0\ninfo:\n  title: T\npaths: {}\ncomponents:\n  x:\n  version: 1\n';
  assert.equal(setOpenapiVersion(spec, '9.99.0'), null);
});

test('setOpenapiVersion writes $-patterns in the version literally', () => {
  assert.equal(setOpenapiVersion(OPENAPI_HEADER, '$&').split('\n')[4], '  version: $&');
});
