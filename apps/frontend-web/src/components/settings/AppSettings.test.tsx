/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { AppSettingsDialog } from './AppSettings';
import { DEFAULT_APP_SETTINGS } from '../../shared/constants';

vi.mock('./hooks/useSettings', () => ({
  useSettings: () => ({
    settings: DEFAULT_APP_SETTINGS,
    setSettings: vi.fn(),
    isSaving: false,
    error: null,
    saveSettings: vi.fn(),
    revertTheme: vi.fn(),
    commitTheme: vi.fn(),
  }),
}));
vi.mock('./GeneralSettings', () => ({ GeneralSettings: () => null }));

describe('AppSettingsDialog version lookup', () => {
  it('retries after a failed lookup when reopened', async () => {
    const getAppVersion = vi.fn()
      .mockResolvedValueOnce('unknown')
      .mockResolvedValueOnce('3.21.2');
    (window as unknown as { API: { getAppVersion: typeof getAppVersion } }).API = { getAppVersion };

    const { rerender } = render(<AppSettingsDialog open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => expect(getAppVersion).toHaveBeenCalledTimes(1));
    rerender(<AppSettingsDialog open={false} onOpenChange={vi.fn()} />);
    rerender(<AppSettingsDialog open={true} onOpenChange={vi.fn()} />);
    await waitFor(() => expect(getAppVersion).toHaveBeenCalledTimes(2));
  });
});
