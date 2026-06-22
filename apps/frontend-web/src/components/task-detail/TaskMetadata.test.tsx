/**
 * Tests for collapseEpicDictBlob — the defensive renderer hardening that stops
 * a leaked stringified plan dict from rendering as an unformatted wall of text
 * in the task-detail Overview tab.
 */

import { describe, it, expect } from 'vitest';
import { collapseEpicDictBlob } from './TaskMetadata';

const POLLUTED =
  "Benchmark scenario api-gateway. Build under scenarios/api-gateway/. " +
  "Correlation epic #{'plan_id': '016-fastapi-api-gateway-with-rate-limiting', " +
  "'epic_title': 'API gateway', 'children': [{'key': 'C1'}, {'key': 'C2'}], " +
  "'effort_estimate': {'story_points': 24}}. See benchmarks/scenarios/api-gateway/brief.md";

describe('collapseEpicDictBlob', () => {
  it('leaves a well-formed description untouched', () => {
    const clean =
      'Benchmark scenario api-gateway. Correlation epic #101. See brief.md';
    expect(collapseEpicDictBlob(clean)).toBe(clean);
  });

  it('returns the input unchanged when there is no #{ blob', () => {
    const md = '## AC#1\n\nReturns 200 from `GET /healthz`.';
    expect(collapseEpicDictBlob(md)).toBe(md);
  });

  it('collapses a stringified plan dict to its plan_id', () => {
    const out = collapseEpicDictBlob(POLLUTED);
    expect(out).toContain(
      'Correlation epic #016-fastapi-api-gateway-with-rate-limiting.'
    );
  });

  it('removes the dict guts entirely (no wall of text)', () => {
    const out = collapseEpicDictBlob(POLLUTED);
    expect(out).not.toContain('children');
    expect(out).not.toContain('effort_estimate');
    expect(out).not.toContain('story_points');
    expect(out).not.toContain('#{');
  });

  it('preserves the surrounding clean prose', () => {
    const out = collapseEpicDictBlob(POLLUTED);
    expect(out).toContain('Benchmark scenario api-gateway.');
    expect(out).toContain('See benchmarks/scenarios/api-gateway/brief.md');
  });

  it('prefers a numeric epic id when present in the blob', () => {
    const withNum =
      "Correlation epic #{'epic_number': 101, 'plan_id': '016-x', 'children': []}.";
    expect(collapseEpicDictBlob(withNum)).toBe('Correlation epic #101.');
  });

  it('falls back to a short marker when no id can be extracted', () => {
    const noId = "Correlation epic #{'foo': 'bar', 'baz': [1, 2]}.";
    expect(collapseEpicDictBlob(noId)).toBe('Correlation epic #….');
  });
});
