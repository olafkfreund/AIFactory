/** @vitest-environment jsdom */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '../../shared/i18n';
import type { Task } from '../../shared/types/task';
import { PipelineBoard, activeStage, dropStatusFor, isDone, isFailed, phaseStrip, stageOf } from './PipelineBoard';
import { useTaskStore } from '../../stores/task-store';

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
  it('supports keyboard dragging between stages', async () => {
    const planning = task('keyboard-plan', 'backlog', 'planning');
    const updateTaskStatus = vi.fn().mockResolvedValue({ success: true });
    window.API = { updateTaskStatus } as unknown as typeof window.API;
    useTaskStore.setState({ tasks: [planning] });
    const rect = (x: number) => ({ x, y: 10, left: x, top: 10, right: x + 80, bottom: 90, width: 80, height: 80, toJSON: () => ({}) });
    const measure = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const stage = this.closest('[data-stage]')?.getAttribute('data-stage');
      return rect(({ plan: 0, code: 100, qa: 200, review: 300 } as Record<string, number>)[stage ?? ''] ?? 0);
    });
    render(<PipelineBoard tasks={[planning]} onTaskClick={vi.fn()} />);
    const handle = screen.getByTestId('task-row-keyboard-plan').firstElementChild!;
    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    await waitFor(() => expect(screen.getByText(/droppable area keyboard-plan/)).toBeInTheDocument());
    fireEvent.keyDown(handle, { key: 'ArrowRight', code: 'ArrowRight' });
    await waitFor(() => expect(screen.getByText(/droppable area code/)).toBeInTheDocument());
    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    await waitFor(() => expect(updateTaskStatus).toHaveBeenCalledWith('keyboard-plan', 'in_progress', undefined));
    measure.mockRestore();
  });

  it('restores status and shows an error after a rejected keyboard drop', async () => {
    const planning = task('rejected-plan', 'backlog', 'planning');
    window.API = { updateTaskStatus: vi.fn().mockResolvedValue({ success: false, error: 'Blocked' }) } as unknown as typeof window.API;
    useTaskStore.setState({ tasks: [planning] });
    const measure = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const stage = this.closest('[data-stage]')?.getAttribute('data-stage');
      const x = ({ plan: 0, code: 100, qa: 200, review: 300 } as Record<string, number>)[stage ?? ''] ?? 0;
      return { x, y: 10, left: x, top: 10, right: x + 80, bottom: 90, width: 80, height: 80, toJSON: () => ({}) };
    });
    render(<PipelineBoard tasks={[planning]} onTaskClick={vi.fn()} />);
    const handle = screen.getByTestId('task-row-rejected-plan').firstElementChild!;
    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    await waitFor(() => expect(screen.getByText(/droppable area rejected-plan/)).toBeInTheDocument());
    fireEvent.keyDown(handle, { key: 'ArrowRight', code: 'ArrowRight' });
    await waitFor(() => expect(screen.getByText(/droppable area code/)).toBeInTheDocument());
    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not move task');
    expect(useTaskStore.getState().tasks[0].status).toBe('backlog');
    measure.mockRestore();
  });

  it('uses the stage status for a keyboard drop on a Done row in Review', () => {
    const completed = task('completed', 'done', 'complete');
    expect(dropStatusFor([completed], 'completed')).toBe('human_review');
    expect(dropStatusFor([completed], 'review')).toBe('human_review');
  });

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
    fireEvent.click(screen.getByRole('button', { name: /Failed \(1\)/i }));
    expect(container.querySelectorAll('.f-column .f-card')).toHaveLength(1);
    expect(screen.queryByTestId('failed-runs')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Filter Code failures/i }));
    fireEvent.click(screen.getByRole('button', { name: /Clear filter/i }));
    expect(container.querySelectorAll('.f-column .f-card')).toHaveLength(1);
    expect(screen.queryByTestId('failed-runs')).toBeNull();
  });
});
