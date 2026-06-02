/**
 * Regression tests for project-context state reconciliation in loadProjects().
 *
 * Bug: selectedProjectId (restored from localStorage) and activeProjectId
 * (restored from tab-state persistence) come from two different sources and
 * could diverge, leaving the kanban board (follows activeProjectId) and the
 * sidebar/dropdown (follow selectedProjectId) showing different projects —
 * and the Radix <Select> wouldn't re-fire to let the user recover.
 *
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useProjectStore, loadProjects } from '../project-store';

const AIF = 'project-aifactory';
const SARC = 'project-sarc';

function project(id: string, name: string) {
  return { id, name, path: `/tmp/${name}` } as never;
}

function mockAPI(tabState: Record<string, unknown>) {
  (window as unknown as { API: Record<string, unknown> }).API = {
    getTabState: vi.fn().mockResolvedValue({ success: true, data: tabState }),
    getProjects: vi
      .fn()
      .mockResolvedValue({ success: true, data: [project(AIF, 'aifactory'), project(SARC, 'sarc')] }),
    saveTabState: vi.fn().mockResolvedValue({ success: true }),
  };
}

describe('loadProjects() project-context reconciliation', () => {
  beforeEach(() => {
    localStorage.clear();
    useProjectStore.setState({
      projects: [],
      selectedProjectId: null,
      activeProjectId: null,
      openProjectIds: [],
      tabOrder: [],
    });
  });

  it('forces selectedProjectId to match the active tab when they diverge', async () => {
    // last-selected says aifactory, but the active tab is sarc (only sarc open)
    localStorage.setItem('lastSelectedProjectId', AIF);
    mockAPI({ openProjectIds: [SARC], activeProjectId: SARC, tabOrder: [SARC] });

    await loadProjects();

    const s = useProjectStore.getState();
    expect(s.activeProjectId).toBe(SARC);
    // selected must follow the active tab — no board/sidebar mismatch
    expect(s.selectedProjectId).toBe(SARC);
  });

  it('opens the last-selected project as the active tab when no tab is active', async () => {
    localStorage.setItem('lastSelectedProjectId', AIF);
    mockAPI({ openProjectIds: [], activeProjectId: null, tabOrder: [] });

    await loadProjects();

    const s = useProjectStore.getState();
    expect(s.selectedProjectId).toBe(AIF);
    expect(s.activeProjectId).toBe(AIF);
    expect(s.openProjectIds).toContain(AIF);
  });
});
