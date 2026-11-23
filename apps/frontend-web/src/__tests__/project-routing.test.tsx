/** @vitest-environment jsdom */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate } from 'react-router';
import type { Project, Task } from '../shared/types';

// Heavy children are stubs that print which project/view they got.
const xterm = vi.hoisted(() => ({ current: null as { disposed: boolean } | null }));
vi.mock('../components/TerminalGrid', async () => {
  const { useEffect, useState } = await import('react');
  return {
    TerminalGrid: (p: { projectPath?: string }) => {
      const [instance] = useState(() => ({ disposed: false }));
      useEffect(() => {
        xterm.current = instance;
        return () => { instance.disposed = true; };
      }, [instance]);
      return <div data-testid="terms">{p.projectPath}</div>;
    },
  };
});
vi.mock('../components/Worktrees', () => ({ Worktrees: () => null }));
vi.mock('../components/context/Context', () => ({ Context: () => null }));
vi.mock('../components/GitHubIssues', () => ({ GitHubIssues: (p: { onOpenSettings: () => void }) => <button onClick={p.onOpenSettings}>Issue settings</button> }));
vi.mock('../components/github-prs/GitHubPRs', () => ({ GitHubPRs: (p: { onOpenSettings: () => void }) => <button onClick={p.onOpenSettings}>PR settings</button> }));
vi.mock('../components/changelog/Changelog', () => ({ Changelog: () => null }));
vi.mock('../components/Insights', () => ({ Insights: () => <div data-testid="insights" /> }));
vi.mock('../components/AgentTools', () => ({ AgentTools: () => null }));
vi.mock('../components/SkillsPage', () => ({ SkillsPage: () => null }));
vi.mock('../components/WelcomeScreen', () => ({ WelcomeScreen: () => <div data-testid="home" /> }));
vi.mock('../components/AddProjectModal', () => ({ AddProjectModal: () => null }));
vi.mock('../components/settings', () => ({
  ProjectSettingsPage: (p: { projectId: string | null }) => <output data-testid="settings-project">{p.projectId ?? 'none'}</output>,
  PreferencesPage: () => <div data-testid="preferences-page" />,
}));
vi.mock('../components/TaskCreationWizard', () => ({ TaskCreationWizard: (p: { projectId: string }) => <output data-testid="wizard-project">{p.projectId}</output> }));
vi.mock('../components/task-detail', () => ({
  TaskDetailModal: (p: { open: boolean; task: Task | null }) =>
    p.open && p.task ? <div data-testid="aifactory-task-detail">{p.task.id}</div> : null,
}));
vi.mock('../components/MissionControl', () => ({ MissionControl: () => null }));
vi.mock('../components/onboarding', () => ({ OnboardingWizard: (p: { onOpenSettings: () => void }) => <button onClick={p.onOpenSettings}>Onboarding settings</button> }));
vi.mock('../components/ProjectSwitchLoadingModal', () => ({ ProjectSwitchLoadingModal: () => null }));
vi.mock('../components/CommandPalette', () => ({ CommandPalette: (p: { open: boolean }) => p.open ? <div data-testid="palette" /> : null }));
vi.mock('../components/ui/toaster', () => ({ Toaster: () => null }));
vi.mock('../pages/EditorPage', () => ({ EditorPage: () => null }));
vi.mock('../pages/LoginPage', () => ({ LoginPage: () => <div data-testid="login" /> }));
vi.mock('../components/task-detail/AgentConsole', () => ({ AgentConsole: (p: { taskId: string }) => <div data-testid="console">{p.taskId}</div> }));
vi.mock('../hooks/useIpc', () => ({ useIpcListeners: () => undefined }));
vi.mock('../lib/api-client', () => ({
  apiRequest: vi.fn().mockResolvedValue({ success: false }),
  get: vi.fn().mockResolvedValue({ success: false }),
}));
vi.mock('../stores/auth-store', async () => {
  const { create } = await import('zustand');
  const useAuthStore = create(() => ({
    isAuthenticated: true,
    checkAuth: () => Promise.resolve(true),
    logout: () => undefined,
  }));
  return { useAuthStore };
});

import '../shared/i18n';
import App from '../App';
import { useProjectStore } from '../stores/project-store';
import { useTaskStore } from '../stores/task-store';
import { useAuthStore } from '../stores/auth-store';

const proj = (id: string, name: string, createdAt: string): Project =>
  ({ id, name, path: `/src/${id}`, autoBuildPath: '.a', createdAt: new Date(createdAt), updatedAt: new Date(createdAt) }) as unknown as Project;
const A = proj('id-alpha', 'Alpha', '2026-01-01');
const B = proj('id-beta', 'Beta', '2026-01-02');

function makeTask(id: string, title: string, projectId: string): Task {
  return {
    id,
    specId: id,
    projectId,
    title,
    description: '',
    status: 'done',
    subtasks: [],
    logs: [],
    createdAt: new Date('2026-01-01'),
    updatedAt: new Date('2026-01-01'),
  };
}

let tasksFor: Record<string, Task[]> = {};
function setApi(tab: Record<string, unknown>) {
  (window as unknown as { API: unknown }).API = new Proxy(
    {
      getTabState: () => Promise.resolve({ success: true, data: tab }),
      getProjects: () => Promise.resolve({ success: true, data: [A, B] }),
      saveTabState: () => Promise.resolve({ success: true }),
      getTasks: (id: string) => Promise.resolve({ success: true, data: tasksFor[id] ?? [] }),
      getSettings: () => Promise.resolve({ success: true, data: { onboardingCompleted: true, theme: 'dark' } }),
      getAppVersion: () => Promise.resolve('3.21.4'),
    } as Record<string, unknown>,
    { get: (t, k: string) => t[k] ?? (() => Promise.resolve({ success: false })) },
  );
}

let nav: ReturnType<typeof useNavigate>;
function Probe() {
  nav = useNavigate();
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}
const renderAt = (path: string) =>
  render(<MemoryRouter initialEntries={[path]}><Probe /><App /></MemoryRouter>);

beforeEach(() => {
  useAuthStore.setState({ isAuthenticated: true });
  xterm.current = null;
  localStorage.clear();
  sessionStorage.clear();
  window.matchMedia = window.matchMedia || ((() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never);
  tasksFor = {
    'id-alpha': [makeTask('t1', 'alpha-task', 'id-alpha')],
    'id-beta': [makeTask('t2', 'beta-task', 'id-beta')],
  };
  useProjectStore.setState({ projects: [], selectedProjectId: null, activeProjectId: null, openProjectIds: [], tabOrder: [] });
  useTaskStore.setState({ tasks: [] });
});

describe('URL-scoped projects', () => {
  it('URL beats the saved active tab and last-selected project', async () => {
    localStorage.setItem('lastSelectedProjectId', 'id-beta');
    setApi({ openProjectIds: ['id-beta'], activeProjectId: 'id-beta', tabOrder: ['id-beta'] });
    renderAt('/p/alpha');
    expect(await screen.findByTestId('task-row-t1')).toBeInTheDocument();
    expect(screen.queryByTestId('task-row-t2')).toBeNull();
    expect(useProjectStore.getState().activeProjectId).toBe('id-alpha');
    expect(useProjectStore.getState().openProjectIds).toContain('id-beta');
    expect(screen.getByTestId('wizard-project')).toHaveTextContent('id-alpha');
  });

  it('rewrites an id in the URL to the canonical slug and preserves search', async () => {
    setApi({});
    renderAt('/p/id-beta?filter=open');
    expect(await screen.findByTestId('task-row-t2')).toBeInTheDocument();
    await waitFor(() => { expect(screen.getByTestId('terms')).toHaveTextContent('/src/id-beta'); });
    await waitFor(() => { expect(screen.getByTestId('location')).toHaveTextContent('/p/beta?filter=open'); });
  });

  it('shows not found for an unknown slug and never mounts views', async () => {
    setApi({});
    renderAt('/p/nope');
    expect(await screen.findByText(/Project not found/)).toBeInTheDocument();
    expect(screen.queryByTestId('task-row-t1')).toBeNull();
  });

  it('keeps an ambiguous bare slug on a chooser instead of picking a twin', async () => {
    const first = proj('id-first', 'Demo', '2026-02-01');
    const second = proj('id-second', 'Demo', '2026-01-01');
    setApi({});
    (window.API as unknown as { getProjects: () => Promise<unknown> }).getProjects = () => Promise.resolve({ success: true, data: [first, second] });
    renderAt('/p/demo/insights');
    expect(await screen.findByText('Choose a project')).toBeInTheDocument();
    expect(screen.queryByTestId('insights')).toBeNull();
    expect(screen.getByRole('link', { name: /id-first/ })).toHaveAttribute('href', '/p/demo-id-first/insights');
    expect(screen.getByRole('link', { name: /id-second/ })).toHaveAttribute('href', '/p/demo-id-second/insights');
  });

  it('back/forward style navigation re-syncs and never shows the other project tasks', async () => {
    setApi({});
    renderAt('/p/alpha');
    expect(await screen.findByTestId('task-row-t1')).toBeInTheDocument();
    act(() => { void nav('/p/beta'); });
    expect(await screen.findByTestId('task-row-t2')).toBeInTheDocument();
    expect(screen.queryByTestId('task-row-t1')).toBeNull();
    act(() => { void nav(-1); });
    expect(await screen.findByTestId('task-row-t1')).toBeInTheDocument();
    expect(useProjectStore.getState().activeProjectId).toBe('id-alpha');
  });

  it('TerminalGrid stays mounted across project and global routes', async () => {
    setApi({});
    renderAt('/p/alpha');
    await waitFor(() => { expect(screen.getByTestId('terms')).toHaveTextContent('/src/id-alpha'); });
    const firstInstance = xterm.current;
    expect(firstInstance).toEqual({ disposed: false });
    act(() => { void nav('/p/beta'); });
    await waitFor(() => { expect(screen.getByTestId('terms')).toHaveTextContent('/src/id-beta'); });
    act(() => { void nav('/projects'); });
    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeInTheDocument();
    act(() => { void nav('/preferences'); });
    expect(await screen.findByTestId('preferences-page')).toBeInTheDocument();
    act(() => { void nav('/p/alpha/terminals'); });
    await waitFor(() => { expect(screen.getByTestId('terms')).toHaveTextContent('/src/id-alpha'); });
    expect(xterm.current).toBe(firstInstance);
    expect(firstInstance?.disposed).toBe(false);
  });

  it('switches projects from the ProjectSwitcher combobox by keyboard and keeps the active view', async () => {
    setApi({});
    renderAt('/p/alpha/insights');
    await screen.findByTestId('insights');
    fireEvent.click(screen.getByRole('button', { name: /Project.*Alpha/ }));
    const search = screen.getByRole('combobox');
    fireEvent.change(search, { target: { value: 'Beta' } });
    fireEvent.keyDown(search, { key: 'Enter' });
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/p/beta/insights'));
    fireEvent.click(screen.getByRole('button', { name: /Project.*Beta/ }));
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' });
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.getByTestId('location')).toHaveTextContent('/p/beta/insights');
  });

  it('numeric shortcuts follow registry order, skip editable fields, and Ctrl+K opens the palette', async () => {
    setApi({});
    renderAt('/p/beta/insights');
    await screen.findByTestId('insights');
    fireEvent.keyDown(window, { key: '1', ctrlKey: true });
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/p/alpha/insights'));
    fireEvent.click(screen.getByRole('button', { name: /Project.*Alpha/ }));
    const search = screen.getByRole('combobox');
    fireEvent.keyDown(search, { key: '2', ctrlKey: true });
    expect(screen.getByTestId('location')).toHaveTextContent('/p/alpha/insights');
    fireEvent.keyDown(search, { key: 'Escape' });
    fireEvent.keyDown(window, { key: '2', metaKey: true });
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/p/beta/insights'));
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    expect(screen.getByTestId('palette')).toBeInTheDocument();
  });

  it('maps Ctrl/⌘+1–9 to all nine registry slots', async () => {
    const nine = Array.from({ length: 9 }, (_, i) => proj(`id-${i + 1}`, `Project${i + 1}`, `2026-01-${String(i + 1).padStart(2, '0')}`));
    setApi({});
    (window.API as unknown as { getProjects: () => Promise<unknown> }).getProjects = () => Promise.resolve({ success: true, data: nine });
    renderAt('/p/project1/insights');
    await screen.findByTestId('insights');
    for (let i = 1; i <= 9; i++) {
      fireEvent.keyDown(window, { key: String(i), ctrlKey: i % 2 === 1, metaKey: i % 2 === 0 });
      await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(`/p/project${i}/insights`));
    }
  });

  it('view segment selects the view', async () => {
    setApi({});
    renderAt('/p/alpha/insights');
    expect(await screen.findByTestId('insights')).toBeInTheDocument();
  });

  it('Tasks click from another view keeps the project', async () => {
    setApi({});
    renderAt('/p/alpha/insights');
    await screen.findByTestId('insights');
    fireEvent.click(screen.getByRole('button', { name: 'Tasks' }));
    expect(await screen.findByTestId('task-row-t1')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/p/alpha');
    expect(screen.queryByTestId('task-row-t2')).toBeNull();
  });

  it('project switch resets an open detail view', async () => {
    setApi({});
    renderAt('/p/alpha');
    await screen.findByTestId('task-row-t1');
    fireEvent.click(screen.getByRole('button', { name: 'alpha-task' }));
    expect(await screen.findByTestId('aifactory-task-detail')).toBeInTheDocument();
    act(() => { void nav('/p/beta'); });
    expect(await screen.findByTestId('task-row-t2')).toBeInTheDocument();
    expect(screen.queryByTestId('aifactory-task-detail')).toBeNull();
  });

  it('/ shows My projects, /projects the Projects page', async () => {
    setApi({});
    const r = renderAt('/');
    expect(await screen.findByTestId('home')).toBeInTheDocument();
    r.unmount();
    renderAt('/projects');
    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeInTheDocument();
    expect(document.querySelectorAll('main')).toHaveLength(1);
  });

  it('unregistering requires typing the project name and calls removeProject', async () => {
    const removed: string[] = [];
    setApi({});
    (window.API as unknown as { removeProject: (id: string) => Promise<unknown> }).removeProject = (id) => {
      removed.push(id);
      return Promise.resolve({ success: true });
    };
    renderAt('/projects');
    const row = (await screen.findByText('Alpha')).closest('div.flex')!;
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: 'Delete' }));
    const confirm = screen.getByRole('button', { name: 'Unregister project' });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Alpha' } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => { expect(removed).toEqual(['id-alpha']); });
    expect(screen.queryByText('Alpha')).toBeNull();
  });

  it('a deep link opened logged out returns to the same URL after login', async () => {
    setApi({});
    useAuthStore.setState({ isAuthenticated: false });
    const r = renderAt('/p/beta/insights?tab=summary');
    expect(await screen.findByTestId('login')).toBeInTheDocument();
    expect(sessionStorage.getItem('aif-return-to')).toBe('/p/beta/insights?tab=summary');
    r.unmount();
    useAuthStore.setState({ isAuthenticated: true });
    renderAt('/');
    expect(await screen.findByTestId('insights')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/p/beta/insights?tab=summary');
    expect(sessionStorage.getItem('aif-return-to')).toBeNull();
  });

  it('settings view stays on the URL project while ambiguous settings keeps the chooser', async () => {
    const first = proj('id-first', 'Demo', '2026-02-01');
    const second = proj('id-second', 'Demo', '2026-01-01');
    setApi({ openProjectIds: ['id-second'], activeProjectId: 'id-second', tabOrder: ['id-second'] });
    (window.API as unknown as { getProjects: () => Promise<unknown> }).getProjects = () => Promise.resolve({ success: true, data: [first, second] });
    renderAt('/p/demo/settings');
    expect(await screen.findByText('Choose a project')).toBeInTheDocument();
    expect(screen.queryByTestId('settings-project')).toBeNull();
    fireEvent.click(screen.getByRole('link', { name: /id-first/ }));
    expect(await screen.findByTestId('settings-project')).toHaveTextContent('id-first');
    expect(screen.getByTestId('location')).toHaveTextContent('/settings');
  });

  it.each([
    ['/p/alpha/github-issues', 'Issue settings', '/p/alpha/settings'],
    ['/p/alpha/github-prs', 'PR settings', '/p/alpha/settings'],
    ['/p/alpha', 'Onboarding settings', '/preferences'],
  ])('routes %s entry point to its settings page', async (path, label, destination) => {
    setApi({});
    renderAt(path);
    fireEvent.click(await screen.findByRole('button', { name: label }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(destination));
  });

  it('shows fetch failure with retry instead of project not found', async () => {
    setApi({});
    let calls = 0;
    (window.API as unknown as { getProjects: () => Promise<unknown> }).getProjects = () => Promise.resolve(
      ++calls === 1 ? { success: false, error: 'Network down' } : { success: true, data: [A, B] },
    );
    renderAt('/p/alpha');
    expect(await screen.findByRole('alert')).toHaveTextContent('Network down');
    expect(screen.queryByText(/Project not found/)).toBeNull();
    fireEvent.click(screen.getByText('Retry'));
    expect(await screen.findByTestId('task-row-t1')).toBeInTheDocument();
  });

  it('shows fetch failure on the projects index too', async () => {
    setApi({});
    (window.API as unknown as { getProjects: () => Promise<unknown> }).getProjects = () => Promise.resolve({ success: false, error: 'Index network down' });
    renderAt('/projects');
    expect(await screen.findByRole('alert')).toHaveTextContent('Index network down');
    expect(screen.queryByRole('heading', { name: 'Projects' })).toBeNull();
  });

  it('waits for console lookup and rejects unknown projects', async () => {
    setApi({});
    let resolve: ((value: unknown) => void) | undefined;
    (window.API as unknown as { getProjects: () => Promise<unknown> }).getProjects = () => new Promise((r) => { resolve = r; });
    renderAt('/p/alpha/console/001');
    expect(screen.queryByTestId('console')).toBeNull();
    await waitFor(() => { expect(resolve).toBeDefined(); });
    await act(async () => { resolve?.({ success: true, data: [A, B] }); });
    expect(await screen.findByTestId('console')).toHaveTextContent('id-alpha:001');
    act(() => { void nav('/p/missing/console/001'); });
    expect(await screen.findByText(/Project not found/)).toBeInTheDocument();
    expect(screen.queryByTestId('console')).toBeNull();
  });

  it('retries a failed standalone console project fetch', async () => {
    setApi({});
    let calls = 0;
    (window.API as unknown as { getProjects: () => Promise<unknown> }).getProjects = () => Promise.resolve(
      ++calls === 1 ? { success: false, error: 'Console network down' } : { success: true, data: [A, B] },
    );
    renderAt('/p/alpha/console/001');
    expect(await screen.findByRole('alert')).toHaveTextContent('Console network down');
    expect(screen.queryByTestId('console')).toBeNull();
    fireEvent.click(screen.getByText('Retry'));
    expect(await screen.findByTestId('console')).toHaveTextContent('id-alpha:001');
  });

  it('legacy console resolves IDs first and preserves search', async () => {
    const collision = { ...A, slug: 'id-beta' };
    setApi({});
    (window.API as unknown as { getProjects: () => Promise<unknown> }).getProjects = () => Promise.resolve({ success: true, data: [collision, B] });
    renderAt('/console/id-beta/001?tail=1');
    expect(await screen.findByTestId('console')).toHaveTextContent('id-beta:001');
    expect(screen.getByTestId('location')).toHaveTextContent('?tail=1');
  });

  it('clearing tasks invalidates a pending request on a route change', async () => {
    setApi({});
    let resolve: (value: unknown) => void = () => undefined;
    (window.API as unknown as { getTasks: (id: string) => Promise<unknown> }).getTasks = () => new Promise((r) => { resolve = r; });
    renderAt('/p/alpha');
    act(() => { void nav('/projects'); });
    await act(async () => { resolve({ success: true, data: tasksFor['id-alpha'] }); });
    expect(useTaskStore.getState().tasks).toEqual([]);
  });

  it('an old response for project A cannot overwrite project B tasks', async () => {
    const { loadTasks } = await import('../stores/task-store');
    let resolveA: (v: unknown) => void = () => undefined;
    const api = { getTasks: (id: string) => id === 'id-alpha' ? new Promise((r) => { resolveA = r; }) : Promise.resolve({ success: true, data: tasksFor[id] }) };
    (window as unknown as { API: unknown }).API = api;
    const pa = loadTasks('id-alpha');
    await loadTasks('id-beta');
    resolveA({ success: true, data: tasksFor['id-alpha'] });
    await pa;
    expect(useTaskStore.getState().tasks.map((t) => t.title)).toEqual(['beta-task']);
  });
});
