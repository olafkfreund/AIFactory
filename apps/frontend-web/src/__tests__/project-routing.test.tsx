/** @vitest-environment jsdom */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { MemoryRouter, useNavigate } from 'react-router';
import type { Project } from '../shared/types';

// Heavy children are stubs that print which project/view they got.
vi.mock('../components/Sidebar', () => ({ Sidebar: () => null }));
vi.mock('../components/ProjectTabBar', () => ({
  ProjectTabBar: (p: { currentSlug?: string }) => <div data-testid="bar">{p.currentSlug}</div>,
}));
vi.mock('../components/pipeline/PipelineBoard', async () => {
  const { useTaskStore } = await import('../stores/task-store');
  return { PipelineBoard: () => <div data-testid="kanban">{useTaskStore.getState().tasks.map((t) => t.title).join(',')}</div> };
});
const mounts = vi.hoisted(() => ({ n: 0 }));
vi.mock('../components/TerminalGrid', async () => {
  const { useEffect } = await import('react');
  return {
    TerminalGrid: (p: { projectPath?: string }) => {
      useEffect(() => { mounts.n++; }, []);
      return <div data-testid="terms">{p.projectPath}</div>;
    },
  };
});
vi.mock('../components/Worktrees', () => ({ Worktrees: () => null }));
vi.mock('../components/context/Context', () => ({ Context: () => null }));
vi.mock('../components/GitHubIssues', () => ({ GitHubIssues: () => null }));
vi.mock('../components/github-prs/GitHubPRs', () => ({ GitHubPRs: () => null }));
vi.mock('../components/changelog/Changelog', () => ({ Changelog: () => null }));
vi.mock('../components/Insights', () => ({ Insights: () => <div data-testid="insights" /> }));
vi.mock('../components/AgentTools', () => ({ AgentTools: () => null }));
vi.mock('../components/SkillsPage', () => ({ SkillsPage: () => null }));
vi.mock('../components/WelcomeScreen', () => ({ WelcomeScreen: () => <div data-testid="home" /> }));
vi.mock('../components/AddProjectModal', () => ({ AddProjectModal: () => null }));
vi.mock('../components/settings', () => ({ AppSettingsDialog: () => null }));
vi.mock('../components/TaskCreationWizard', () => ({ TaskCreationWizard: () => null }));
vi.mock('../components/task-detail', () => ({ TaskDetailModal: () => null }));
vi.mock('../components/MissionControl', () => ({ MissionControl: () => null }));
vi.mock('../components/onboarding', () => ({ OnboardingWizard: () => null }));
vi.mock('../components/ProjectSwitchLoadingModal', () => ({ ProjectSwitchLoadingModal: () => null }));
vi.mock('../components/CommandPalette', () => ({ CommandPalette: () => null }));
vi.mock('../components/ui/toaster', () => ({ Toaster: () => null }));
vi.mock('../pages/EditorPage', () => ({ EditorPage: () => null }));
vi.mock('../pages/ProjectsPage', () => ({ ProjectsPage: () => <div data-testid="projects-page" /> }));
vi.mock('../pages/LoginPage', () => ({ LoginPage: () => <div data-testid="login" /> }));
vi.mock('../pages/ConsolePage', () => ({ ConsolePage: () => <div data-testid="console" /> }));
vi.mock('../pages/ConsoleGridPage', () => ({ ConsoleGridPage: () => <div data-testid="console-grid" /> }));
vi.mock('../hooks/useIpc', () => ({ useIpcListeners: () => undefined }));
vi.mock('../lib/api-client', () => ({ apiRequest: vi.fn().mockResolvedValue({ success: false }) }));
vi.mock('../stores/auth-store', async () => {
  const { create } = await import('zustand');
  const useAuthStore = create(() => ({ isAuthenticated: true, checkAuth: () => Promise.resolve(true) }));
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

let tasksFor: Record<string, { id: string; title: string; projectId: string }[]> = {};
function setApi(tab: Record<string, unknown>) {
  (window as unknown as { API: unknown }).API = new Proxy(
    {
      getTabState: () => Promise.resolve({ success: true, data: tab }),
      getProjects: () => Promise.resolve({ success: true, data: [A, B] }),
      saveTabState: () => Promise.resolve({ success: true }),
      getTasks: (id: string) => Promise.resolve({ success: true, data: tasksFor[id] ?? [] }),
      getSettings: () => Promise.resolve({ success: true, data: { onboardingCompleted: true, theme: 'dark' } }),
    } as Record<string, unknown>,
    { get: (t, k: string) => t[k] ?? (() => Promise.resolve({ success: false })) },
  );
}

let nav: ReturnType<typeof useNavigate>;
function Probe() { nav = useNavigate(); return null; }
const renderAt = (path: string) =>
  render(<MemoryRouter initialEntries={[path]}><Probe /><App /></MemoryRouter>);

beforeEach(() => {
  useAuthStore.setState({ isAuthenticated: true });
  mounts.n = 0;
  localStorage.clear();
  sessionStorage.clear();
  window.matchMedia = window.matchMedia || ((() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never);
  tasksFor = {
    'id-alpha': [{ id: 't1', title: 'alpha-task', projectId: 'id-alpha' }],
    'id-beta': [{ id: 't2', title: 'beta-task', projectId: 'id-beta' }],
  };
  useProjectStore.setState({ projects: [], selectedProjectId: null, activeProjectId: null, openProjectIds: [], tabOrder: [] });
  useTaskStore.setState({ tasks: [] });
});

describe('URL-scoped projects', () => {
  it('URL beats the saved active tab and last-selected project', async () => {
    localStorage.setItem('lastSelectedProjectId', 'id-beta');
    setApi({ openProjectIds: ['id-beta'], activeProjectId: 'id-beta', tabOrder: ['id-beta'] });
    renderAt('/p/alpha');
    await waitFor(() => { expect(screen.getByTestId('kanban')).toHaveTextContent('alpha-task'); });
    expect(useProjectStore.getState().activeProjectId).toBe('id-alpha');
    // the saved tab is still restored as an open tab
    expect(useProjectStore.getState().openProjectIds).toContain('id-beta');
  });

  it('rewrites an id in the URL to the canonical slug', async () => {
    setApi({});
    renderAt('/p/id-beta');
    await waitFor(() => { expect(screen.getByTestId('kanban')).toHaveTextContent('beta-task'); });
    await waitFor(() => { expect(screen.getByTestId('terms')).toHaveTextContent('/src/id-beta'); });
  });

  it('shows not found for an unknown slug and never mounts views', async () => {
    setApi({});
    renderAt('/p/nope');
    expect(await screen.findByText(/Project not found/)).toBeInTheDocument();
    expect(screen.queryByTestId('kanban')).toBeNull();
  });

  it('back/forward style navigation re-syncs and never shows the other project tasks', async () => {
    setApi({});
    renderAt('/p/alpha');
    await waitFor(() => { expect(screen.getByTestId('kanban')).toHaveTextContent('alpha-task'); });
    act(() => { void nav('/p/beta'); });
    await waitFor(() => { expect(screen.getByTestId('kanban')).toHaveTextContent('beta-task'); });
    expect(screen.getByTestId('kanban')).not.toHaveTextContent('alpha-task');
    act(() => { void nav(-1); });
    await waitFor(() => { expect(screen.getByTestId('kanban')).toHaveTextContent('alpha-task'); });
    expect(useProjectStore.getState().activeProjectId).toBe('id-alpha');
  });

  it('TerminalGrid is not remounted by a project switch', async () => {
    setApi({});
    renderAt('/p/alpha');
    await waitFor(() => { expect(screen.getByTestId('terms')).toHaveTextContent('/src/id-alpha'); });
    act(() => { void nav('/p/beta'); });
    await waitFor(() => { expect(screen.getByTestId('terms')).toHaveTextContent('/src/id-beta'); });
    expect(mounts.n).toBe(1);
  });

  it('view segment selects the view', async () => {
    setApi({});
    renderAt('/p/alpha/insights');
    expect(await screen.findByTestId('insights')).toBeInTheDocument();
  });

  it('/ shows My projects, /projects the Projects page', async () => {
    setApi({});
    const r = renderAt('/');
    expect(await screen.findByTestId('home')).toBeInTheDocument();
    r.unmount();
    renderAt('/projects');
    expect(await screen.findByTestId('projects-page')).toBeInTheDocument();
  });

  it('a deep link opened logged out returns to the same URL after login', async () => {
    setApi({});
    useAuthStore.setState({ isAuthenticated: false });
    const r = renderAt('/p/beta/insights');
    expect(await screen.findByTestId('login')).toBeInTheDocument();
    expect(sessionStorage.getItem('aif-return-to')).toBe('/p/beta/insights');
    r.unmount();
    useAuthStore.setState({ isAuthenticated: true });
    renderAt('/');
    expect(await screen.findByTestId('insights')).toBeInTheDocument();
    expect(sessionStorage.getItem('aif-return-to')).toBeNull();
  });

  it('legacy console link redirects to /p/:slug/console', async () => {
    setApi({});
    renderAt('/console/id-alpha');
    expect(await screen.findByTestId('console-grid')).toBeInTheDocument();
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
