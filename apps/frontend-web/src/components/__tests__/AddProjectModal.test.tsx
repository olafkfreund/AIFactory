/**
 * @vitest-environment jsdom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AddProjectModal } from '../AddProjectModal';
import type { API } from '../../shared/types';

describe('AddProjectModal (B10)', () => {
  const discoverProjects = vi.fn();
  const getDefaultProjectLocation = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    discoverProjects.mockResolvedValue({
      success: true,
      data: [
        {
          name: 'demo-repo',
          path: '/var/lib/aifactory/workspaces/demo-repo',
          has_git: true,
          has_package_json: true,
          has_requirements: false,
          has_aifactory: true,
          has_claude_md: true,
        },
      ],
    });
    getDefaultProjectLocation.mockResolvedValue('/var/lib/aifactory/workspaces');

    (window as unknown as { API: Partial<API> }).API = {
      discoverProjects,
      getDefaultProjectLocation,
    };
  });

  it('opens in clone mode with no local project scan', () => {
    render(<AddProjectModal open={true} onOpenChange={vi.fn()} />);

    expect(screen.getByLabelText(/Git repository URL/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Projects Folder/i)).not.toBeInTheDocument();
    expect(discoverProjects).not.toHaveBeenCalled();
    expect(getDefaultProjectLocation).not.toHaveBeenCalled();
  });

  it('scans from the server default location when the user switches to Local mode', async () => {
    render(<AddProjectModal open={true} onOpenChange={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: /Local folder/i }));

    await waitFor(() => {
      expect(getDefaultProjectLocation).toHaveBeenCalledTimes(1);
      expect(discoverProjects).toHaveBeenCalledWith('/var/lib/aifactory/workspaces', 2);
    });

    expect(screen.getByLabelText(/Projects Folder/i)).toHaveValue(
      '/var/lib/aifactory/workspaces'
    );
    expect(await screen.findByText('demo-repo')).toBeInTheDocument();
  });

  it('preserves form inputs when switching between clone and local modes', async () => {
    render(<AddProjectModal open={true} onOpenChange={vi.fn()} />);

    const gitUrlInput = screen.getByLabelText(/Git repository URL/i);
    fireEvent.change(gitUrlInput, {
      target: { value: 'https://github.com/example/project.git' },
    });

    // Switch to Local folder mode
    fireEvent.click(screen.getByRole('button', { name: /Local folder/i }));
    await waitFor(() => {
      expect(discoverProjects).toHaveBeenCalledTimes(1);
    });

    const folderInput = screen.getByLabelText(/Projects Folder/i);
    fireEvent.change(folderInput, {
      target: { value: '/var/lib/aifactory/workspaces/custom' },
    });

    // Switch back to Clone mode — Git URL is still intact
    fireEvent.click(screen.getByRole('button', { name: /Clone from Git URL/i }));
    expect(screen.getByLabelText(/Git repository URL/i)).toHaveValue(
      'https://github.com/example/project.git'
    );

    // Switch back to Local mode — custom folder is still intact and no redundant default scan runs
    fireEvent.click(screen.getByRole('button', { name: /Local folder/i }));
    expect(screen.getByLabelText(/Projects Folder/i)).toHaveValue(
      '/var/lib/aifactory/workspaces/custom'
    );
    expect(getDefaultProjectLocation).toHaveBeenCalledTimes(1);
    expect(discoverProjects).toHaveBeenCalledTimes(1);
  });
});
