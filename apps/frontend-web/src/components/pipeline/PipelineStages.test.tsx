/** @vitest-environment jsdom */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '../../shared/i18n';
import type { Task } from '../../shared/types/task';
import { PipelineBoard, activeStage, isDone, isFailed, phaseStrip, stageOf } from './PipelineBoard';

function task(
  id: string,
  status: Task['status'],
  phase: NonNullable<Task['executionProgress']>['phase'],
  reviewReason?: Task['reviewReason']
): Task {
  return {
    id,
    specId: id,
    projectId: 'project',
    title: id,
    description: '',
    status,
    reviewReason,
    subtasks: [],
    logs: [],
    createdAt: new Date(),
    updatedAt: new Date(),
    executionProgress: { phase, phaseProgress: 0, overallProgress: 0 },
  };
}

describe('PipelineBoard stage helpers', () => {
  it('ignores stale phases on done tasks while showing the active QA task', () => {
    const doneTasks = [
      task('old plan', 'done', 'planning'),
      task('old code', 'done', 'coding'),
      task('old review', 'done', 'qa_review'),
    ];
    const liveTask = task('live review', 'ai_review', 'qa_review');

    for (const d of doneTasks) {
      expect(isDone(d)).toBe(true);
      expect(stageOf(d)).toBe('review');
      expect(activeStage(d)).toBeNull();
    }

    expect(isDone(liveTask)).toBe(false);
    expect(stageOf(liveTask)).toBe('qa');
    expect(activeStage(liveTask)).toBe('qa');
    expect(phaseStrip(liveTask)).toEqual([
      { key: 'plan', label: 'Plan', state: 'done' },
      { key: 'code', label: 'Code', state: 'done' },
      { key: 'qa', label: 'QA', state: 'active' },
      { key: 'review', label: 'Review', state: 'pending' },
    ]);

    render(<PipelineBoard tasks={[...doneTasks, liveTask]} onTaskClick={vi.fn()} onNewTaskClick={vi.fn()} />);
    expect(screen.getByRole('toolbar', { name: /Pipeline stages/i })).toBeInTheDocument();
  });

  it('classifies failures and filters via the PipelineStrip', () => {
    const running = task('running-1', 'in_progress', 'coding');
    const failed = task('failed-1', 'human_review', 'failed', 'errors');

    expect(isFailed(failed)).toBe(true);
    expect(stageOf(failed)).toBe('code');

    const { container } = render(
      <PipelineBoard tasks={[running, failed]} onTaskClick={vi.fn()} onNewTaskClick={vi.fn()} />
    );

    expect(container.querySelectorAll('.f-column .f-card')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: /Filter Code failures/i }));
    expect(container.querySelectorAll('.f-column .f-card')).toHaveLength(0);
    expect(screen.getByTestId('failed-runs')).toHaveTextContent('failed-1');
    fireEvent.click(screen.getByRole('button', { name: /Clear filter/i }));
    expect(container.querySelectorAll('.f-column .f-card')).toHaveLength(1);
    expect(screen.queryByTestId('failed-runs')).toBeNull();
  });
});
