/** @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import '../../shared/i18n';
import { useProjectStore } from '../../stores/project-store';
import { ProjectSettingsPage } from './AppSettings';
import type { UseProjectSettingsReturn } from '../project-settings/hooks/useProjectSettings';
import type { Project } from '../../shared/types';

const save = vi.hoisted(() => vi.fn());
vi.mock('./ProjectSettingsContent', () => ({
  ProjectSettingsContent: ({ onHookReady }: { onHookReady: (hook: UseProjectSettingsReturn | null) => void }) => {
    useEffect(() => { onHookReady({ handleSave: save, isSaving: false } as unknown as UseProjectSettingsReturn); }, [onHookReady]);
    return null;
  },
}));

beforeEach(() => {
  save.mockReset();
  useProjectStore.setState({ projects: [{ id: 'alpha', name: 'Alpha', path: '/alpha' } as Project] });
});

describe('ProjectSettingsPage save', () => {
  it('disables and ignores duplicate submissions until the first save settles', async () => {
    let finish: () => void = () => undefined;
    save.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(<ProjectSettingsPage projectId="alpha" />);
    const button = screen.getByRole('button', { name: /Save Settings/i });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(save).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    finish();
    await waitFor(() => expect(button).not.toBeDisabled());
  });
});
