import { describe, expect, it } from 'vitest';
import { isIncompleteHumanReview } from '../task-store';
import { REVIEW_REASON_BADGES } from '../../shared/constants';
import type { ReviewReason, Task } from '../../shared/types';

// Factory#2586: the merger records a task's PR state as a review reason.
function reviewTask(reviewReason: ReviewReason): Task {
  return { status: 'human_review', reviewReason, subtasks: [] } as unknown as Task;
}

describe('isIncompleteHumanReview', () => {
  it.each<ReviewReason>(['awaiting_merge', 'pr_closed', 'no_work'])(
    'defers to the PR state for %s instead of advising a resume',
    (reason) => {
      expect(isIncompleteHumanReview(reviewTask(reason))).toBe(false);
    },
  );

  it('still flags a crashed build with no completed subtasks', () => {
    expect(isIncompleteHumanReview(reviewTask('errors'))).toBe(true);
  });
});

describe('REVIEW_REASON_BADGES', () => {
  it('never labels a PR-state reason as QA Issues', () => {
    for (const reason of ['awaiting_merge', 'pr_closed', 'no_work'] as const) {
      expect(REVIEW_REASON_BADGES[reason].label).not.toBe('QA Issues');
    }
  });
});
