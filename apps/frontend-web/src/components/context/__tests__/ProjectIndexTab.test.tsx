/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { TooltipProvider } from '../../ui/tooltip';
import { ProjectIndexTab } from '../ProjectIndexTab';
import type { ProjectIndex } from '../../../shared/types';

describe('ProjectIndexTab and ServiceCard relative paths (B17)', () => {
  it('displays project_root as the project name and service paths/entry points relative to project_root', () => {
    const projectIndex: ProjectIndex = {
      project_root: '/var/lib/aifactory/workspaces/org-42/my-service-repo',
      project_type: 'monorepo',
      services: {
        backend: {
          name: 'backend',
          type: 'backend',
          path: '/var/lib/aifactory/workspaces/org-42/my-service-repo/apps/backend',
          entry_point: '/var/lib/aifactory/workspaces/org-42/my-service-repo/apps/backend/server/main.py',
          language: 'Python',
        },
        rootApp: {
          name: 'rootApp',
          type: 'frontend',
          path: '/var/lib/aifactory/workspaces/org-42/my-service-repo',
          entry_point: 'src/index.tsx',
          language: 'TypeScript',
        },
      },
      infrastructure: {},
      conventions: {},
    };

    render(
      <TooltipProvider>
        <ProjectIndexTab
          projectIndex={projectIndex}
          indexLoading={false}
          indexError={null}
          onRefresh={vi.fn()}
        />
      </TooltipProvider>
    );

    // Root itself is rendered as the project name (can appear in overview and root service card)
    expect(screen.getAllByText('my-service-repo').length).toBeGreaterThanOrEqual(1);
    // Relative service path and entry point
    expect(screen.getByText('apps/backend')).toBeInTheDocument();
    expect(screen.getByText('apps/backend/server/main.py')).toBeInTheDocument();
    expect(screen.getByText('src/index.tsx')).toBeInTheDocument();
    // Raw pod root path is never displayed
    expect(
      screen.queryByText(/\/var\/lib\/aifactory\/workspaces/)
    ).not.toBeInTheDocument();
  });
});
