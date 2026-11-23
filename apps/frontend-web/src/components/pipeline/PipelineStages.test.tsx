import { render } from '@testing-library/react';
import { beforeAll, expect, it, vi } from 'vitest';
import type { Task } from '../../shared/types/task';
import { PipelineBoard } from './PipelineBoard';
import { PipelineHeader } from './PipelineHeader';

vi.mock('../TaskCard', () => ({ TaskCard: () => <div>Task card</div> }));

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

function task(id: string, status: Task['status'], phase: NonNullable<Task['executionProgress']>['phase']): Task {
  return {
    id, specId: id, projectId: 'project', title: id, description: '', status,
    subtasks: [], logs: [], createdAt: new Date(), updatedAt: new Date(),
    executionProgress: { phase, phaseProgress: 0, overallProgress: 0 },
  };
}

function stage(container: HTMLElement, label: string): Element | undefined {
  return [...container.querySelectorAll('.pl-stage')]
    .find((element) => element.querySelector('.pl-label')?.textContent === label);
}

it('ignores stale phases on done tasks while showing the active review task', () => {
  const doneTasks = [
    task('old plan', 'done', 'planning'),
    task('old code', 'done', 'coding'),
    task('old review', 'done', 'qa_review'),
  ];
  const liveTask = task('live review', 'ai_review', 'qa_review');

  const board = render(<PipelineBoard tasks={doneTasks} onTaskClick={vi.fn()} onNewTaskClick={vi.fn()} />);
  const rail = board.container.querySelector('.pl-railpanel') as HTMLElement;
  expect(stage(rail, 'Plan')).not.toHaveClass('is-active');
  expect(stage(rail, 'Code')).not.toHaveClass('is-active');
  expect(stage(rail, 'Review')).not.toHaveClass('is-active');
  expect(board.container.querySelectorAll('.pl-card-wrap.is-live')).toHaveLength(0);
  board.rerender(<PipelineBoard tasks={[...doneTasks, liveTask]} onTaskClick={vi.fn()} onNewTaskClick={vi.fn()} />);
  expect(stage(rail, 'Review')).toHaveClass('is-active');
  expect(board.container.querySelectorAll('.pl-card-wrap.is-live')).toHaveLength(1);

  const header = render(<PipelineHeader tasks={doneTasks} />);
  expect(stage(header.container, 'Plan')).not.toHaveClass('is-active');
  expect(stage(header.container, 'Code')).not.toHaveClass('is-active');
  expect(stage(header.container, 'Test')).not.toHaveClass('is-active');
  header.rerender(<PipelineHeader tasks={[...doneTasks, liveTask]} />);
  expect(stage(header.container, 'Test')).toHaveClass('is-active');
  expect(header.getByRole('img')).toHaveAttribute('aria-label', 'Pipeline: 0 planning, 0 coding with 0 active agents, 1 testing');
});
