/** @vitest-environment jsdom */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, renderHook, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '../shared/i18n';
import App from '../App';
import { useAuthStore } from '../stores/auth-store';
import { useProjectStore } from '../stores/project-store';
import { useSettingsStore } from '../stores/settings-store';
import { useTaskStore } from '../stores/task-store';
import type { Project, Task } from '../shared/types';
import { useToast } from '../hooks/use-toast';

vi.mock('../hooks/useIpc', () => ({ useIpcListeners: () => undefined }));
vi.mock('../lib/api-client', () => ({
  apiRequest: vi.fn().mockResolvedValue({ success: true, data: { count: 2 } }),
  get: vi.fn().mockResolvedValue({ success: false }),
}));
vi.mock('../components/TerminalGrid', () => ({ TerminalGrid: () => null }));
vi.mock('../components/ui/toaster', () => ({ Toaster: () => null }));

const DEMO_PROJECT: Project = {
  id: 'id-alpha',
  name: 'Alpha',
  path: '/src/alpha',
  autoBuildPath: '.aifactory',
  settings: {
    model: 'claude-sonnet-4-5-20250929',
    memoryBackend: 'file',
    notifications: {
      onTaskComplete: true,
      onTaskFailed: true,
      onReviewNeeded: true,
      sound: false,
      emailEnabled: false,
    },
    graphitiMcpEnabled: false,
  },
  createdAt: new Date('2026-01-01'),
  updatedAt: new Date('2026-01-01'),
};

function makeTask(overrides: Partial<Task> & { id: string }): Task {
  return {
    specId: overrides.id,
    projectId: 'id-alpha',
    title: overrides.id,
    description: '',
    status: 'backlog',
    subtasks: [],
    logs: [],
    createdAt: new Date('2026-05-28T10:00:00Z'),
    updatedAt: new Date('2026-05-28T10:00:00Z'),
    ...overrides,
  };
}

let mockTasks: Task[] = [];
let apiTarget: Record<string, unknown> = {};

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
  useAuthStore.setState({ isAuthenticated: true, checkAuth: () => Promise.resolve(true) });
  useProjectStore.setState({
    projects: [],
    selectedProjectId: null,
    activeProjectId: null,
    openProjectIds: [],
    tabOrder: [],
  });
  useTaskStore.setState({ tasks: [] });
  mockTasks = [
    makeTask({ id: 'spec-1', status: 'done' }),
  ];
  apiTarget = {
    getTabState: () => Promise.resolve({ success: true, data: {} }),
    saveTabState: () => Promise.resolve({ success: true }),
    getProjects: () => Promise.resolve({ success: true, data: [DEMO_PROJECT] }),
    getTasks: () => Promise.resolve({ success: true, data: mockTasks }),
    getSettings: () => Promise.resolve({ success: true, data: { onboardingCompleted: true, theme: 'dark' } }),
    getAppVersion: () => Promise.resolve('3.21.4'),
    checkClaudeCodeVersion: () => Promise.resolve({ success: true, data: { installed: '1.0.0' } }),
    getAuthStatus: () => Promise.resolve({ success: true, data: { hasToken: true } }),
    checkGitStatus: () => Promise.resolve({ success: true, data: { isGitRepo: true, hasCommits: true } }),
    getProjectEnv: () => Promise.resolve({ success: true, data: { githubEnabled: true, gitProvider: 'github' } }),
    updateTaskStatus: () => Promise.resolve({ success: true }),
    startTask: () => Promise.resolve({ success: true }),
    stopTask: () => Promise.resolve({ success: true }),
    archiveTasks: () => Promise.resolve({ success: true }),
    checkTaskRunning: () => Promise.resolve({ success: true, data: true }),
  };
  (window as unknown as { API: unknown }).API = new Proxy(apiTarget, {
    get: (t, k: string) => t[k] ?? (() => Promise.resolve({ success: false })),
  });
});

describe('AIFactory adapted AppShell', () => {
  it('renders AppShell with portal="build", PipelineStrip, ProjectSwitcher, primary + Task action, and routes to Project Settings and Preferences', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/p/alpha']}>
        <App />
      </MemoryRouter>
    );

    const shell = await waitFor(() => {
      const el = container.querySelector('.f-shell');
      expect(el).toHaveAttribute('data-portal', 'build');
      return el!;
    });
    expect(shell).toBeInTheDocument();

    // PipelineStrip rendered in the shell above the page when on overview
    expect(await screen.findByRole('toolbar', { name: /Pipeline stages/i })).toBeInTheDocument();

    // Primary action "+ Task"
    expect(screen.getByRole('button', { name: '+ Task' })).toBeEnabled();

    // Project Settings pinned in sidebar navigates to /p/alpha/settings with General, Git, Memory, MCP tabs
    fireEvent.click(screen.getByRole('button', { name: 'Project Settings' }));
    expect(await screen.findByTestId('project-settings-page')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'General' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Git' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Memory' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'MCP' })).toBeInTheDocument();
    // Strip is hidden on settings pages
    expect(screen.queryByRole('toolbar', { name: /Pipeline stages/i })).toBeNull();

    // User menu Preferences navigates to /preferences
    fireEvent.click(screen.getByRole('button', { name: 'Preferences' }));
    expect(await screen.findByTestId('preferences-page')).toBeInTheDocument();
  });

  it('filters only failed Code tasks through the shell strip and clears both filters', async () => {
    mockTasks = [
      makeTask({
        id: 'running',
        status: 'in_progress',
        executionProgress: { phase: 'coding', phaseProgress: 50, overallProgress: 50 },
      }),
      makeTask({
        id: 'failed',
        status: 'human_review',
        reviewReason: 'errors',
        executionProgress: { phase: 'failed', phaseProgress: 0, overallProgress: 0 },
      }),
    ];
    const { container } = render(
      <MemoryRouter initialEntries={['/p/alpha']}>
        <App />
      </MemoryRouter>
    );
    await screen.findByTestId('task-row-running');
    fireEvent.click(await screen.findByRole('button', { name: /Filter Code failures/i }));
    expect(container.querySelectorAll('.f-column .f-card')).toHaveLength(0);
    expect(screen.getByTestId('failed-runs')).toHaveTextContent('failed');
    fireEvent.click(screen.getByRole('button', { name: /Clear filter/i }));
    expect(container.querySelectorAll('.f-column .f-card')).toHaveLength(1);
    expect(screen.queryByTestId('failed-runs')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Files' }));
    expect(screen.getByRole('toolbar', { name: /Pipeline stages/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Project.*Alpha/i }));
    fireEvent.click(screen.getByRole('button', { name: 'All projects' }));
    expect(await screen.findByRole('toolbar', { name: /Pipeline stages/i })).toBeInTheDocument();
  });

  it('reverts an unsaved appearance preview on route change', async () => {
    render(
      <MemoryRouter initialEntries={['/preferences']}>
        <App />
      </MemoryRouter>
    );
    await screen.findByTestId('preferences-page');
    fireEvent.click(screen.getByRole('button', { name: /Appearance/i }));
    fireEvent.click(within(screen.getByTestId('preferences-page')).getByRole('button', { name: /light/i }));
    expect(useSettingsStore.getState().settings.theme).toBe('light');
    fireEvent.click(screen.getByRole('button', { name: 'Tasks' }));
    await waitFor(() => expect(useSettingsStore.getState().settings.theme).toBe('dark'));
  });

  it('keeps a top-bar theme choice saved while Preferences is open', async () => {
    render(<MemoryRouter initialEntries={['/preferences']}><App /></MemoryRouter>);
    await screen.findByTestId('preferences-page');
    fireEvent.click(screen.getByRole('button', { name: /Light/ }));
    await waitFor(() => expect(useSettingsStore.getState().settings.theme).toBe('light'));
    fireEvent.click(screen.getByRole('button', { name: 'Tasks' }));
    expect(useSettingsStore.getState().settings.theme).toBe('light');
  });

  it('handles item actions in pending and rejected states on failed WorkCards', async () => {
    mockTasks = [
      makeTask({
        id: 'fail-task',
        status: 'human_review',
        reviewReason: 'errors',
        subtasks: [{ id: 's1', title: 's1', status: 'completed', files: [] }],
        executionProgress: { phase: 'failed', phaseProgress: 0, overallProgress: 0 },
      }),
    ];
    let resolveArchive: ((val: { success: boolean; error?: string }) => void) | undefined;
    apiTarget.archiveTasks = () =>
      new Promise<{ success: boolean; error?: string }>((r) => {
        resolveArchive = r;
      });

    render(
      <MemoryRouter initialEntries={['/p/alpha']}>
        <App />
      </MemoryRouter>
    );

    fireEvent.click(await screen.findByRole('button', { name: /Filter Code failures/i }));
    const failedSection = await screen.findByTestId('failed-runs');
    const archiveBtn = within(failedSection).getByRole('button', { name: 'Archive' });

    fireEvent.click(archiveBtn);
    await waitFor(() => expect(archiveBtn).toBeDisabled());
    expect(archiveBtn).toHaveAttribute('aria-busy', 'true');

    resolveArchive?.({ success: false, error: 'Disk full' });
    await waitFor(() => expect(archiveBtn).not.toBeDisabled());
    const { result } = renderHook(() => useToast());
    expect(result.current.toasts[0]?.description).toBe('Disk full');
  });

  it('limits collapsed done rows to eight, remembers expansion, and opens Copilot links', async () => {
    mockTasks = [
      ...Array.from({ length: 10 }, (_, index) => makeTask({ id: `done-${index}`, status: 'done', createdAt: new Date(2026, 0, index + 1) })),
      makeTask({ id: 'copilot-issue', status: 'copilot_running', metadata: { copilotDispatch: { issue_url: 'https://example.com/issues/1', issue_number: 1 } } }),
      makeTask({ id: 'copilot-pr', status: 'copilot_pr_opened', metadata: { copilotDispatch: { pr_url: 'https://example.com/pull/2', pr_number: 2 } } }),
    ];
    render(<MemoryRouter initialEntries={['/p/alpha']}><App /></MemoryRouter>);
    await screen.findByTestId('task-row-done-9');
    expect(screen.queryByTestId('task-row-done-0')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show 2 more' }));
    expect(screen.getByTestId('task-row-done-0')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Expand done-9' }));
    expect(localStorage.getItem('factory-ui:work-expansion:"id-alpha"')).toContain('done-9');
    fireEvent.click(screen.getByRole('button', { name: 'Expand copilot-issue' }));
    expect(screen.getByRole('link', { name: '#1' })).toHaveAttribute('href', 'https://example.com/issues/1');
    fireEvent.click(screen.getByRole('button', { name: 'Expand copilot-pr' }));
    expect(screen.getByRole('link', { name: 'PR #2' })).toHaveAttribute('href', 'https://example.com/pull/2');
  });
});
