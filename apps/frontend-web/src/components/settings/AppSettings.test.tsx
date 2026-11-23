/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { PreferencesPage } from './AppSettings';
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

describe('PreferencesPage version lookup', () => {
  it('fetches the app version on mount and retries when remounted', async () => {
    const getAppVersion = vi.fn()
      .mockResolvedValueOnce('unknown')
      .mockResolvedValueOnce('3.21.2');
    (window as unknown as { API: { getAppVersion: typeof getAppVersion } }).API = { getAppVersion };

    const { unmount } = render(<PreferencesPage />);
    await waitFor(() => expect(getAppVersion).toHaveBeenCalledTimes(1));
    unmount();
    render(<PreferencesPage />);
    await waitFor(() => expect(getAppVersion).toHaveBeenCalledTimes(2));
  });
});
