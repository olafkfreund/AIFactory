/** @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { MemoryRouter } from 'react-router';
import { useProjectStore } from '../stores/project-store';
import { useTaskStore } from '../stores/task-store';
import type { Project, Task } from '../shared/types';
import { NeedsInputPage } from './NeedsInputPage';

vi.mock('../components/task-detail/NeedsInputBanner', () => ({
  NeedsInputBanner: ({ taskId }: { taskId: string }) => <div>Banner {taskId}</div>,
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => ({ 'needsInput.pageTitle': 'Tasks needing input', 'needsInput.empty': 'No tasks need input' })[key] ?? `UNKNOWN:${key}` }),
}));

const project = { id: 'p1', name: 'Export service', slug: 'export-service' } as Project;
const task = (id: string, reviewReason: Task['reviewReason'], projectId = 'p1') =>
  ({ id, title: `Task ${id}`, status: 'human_review', reviewReason, projectId }) as Task;

afterEach(() => {
  useProjectStore.setState({ projects: [] });
  useTaskStore.setState({ tasks: [] });
});

describe('NeedsInputPage', () => {
  it('lists only visible tasks waiting for input with task links', () => {
    useProjectStore.setState({ projects: [project] });
    useTaskStore.setState({ tasks: [
      task('one', 'needs_input'), task('two', 'approval_rejected'), task('three', 'needs_input', 'hidden'),
    ] });
    render(<MemoryRouter><NeedsInputPage /></MemoryRouter>);
    expect(screen.getByText('Export service')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Task one' })).toHaveAttribute('href', '/p/export-service/tasks/one');
    expect(screen.getByText('Banner one')).toBeInTheDocument();
    expect(screen.queryByText('Task two')).not.toBeInTheDocument();
    expect(screen.queryByText('Task three')).not.toBeInTheDocument();
  });

  it('shows an empty state', () => {
    useProjectStore.setState({ projects: [project] });
    useTaskStore.setState({ tasks: [task('two', 'approval_rejected')] });
    render(<MemoryRouter><NeedsInputPage /></MemoryRouter>);
    expect(screen.getByText('No tasks need input')).toBeInTheDocument();
  });
});
