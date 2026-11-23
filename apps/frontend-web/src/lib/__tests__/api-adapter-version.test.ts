import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as apiClient from '../api-client';
import { resetAppVersionCache, webAPI } from '../api-adapter';

vi.mock('../websocket', () => ({
  wsManager: {
    subscribe: vi.fn(() => () => {}),
    onConnect: vi.fn(() => () => {}),
  },
  terminalWs: {
    subscribe: vi.fn(() => () => {}),
    send: vi.fn(),
  },
}));

describe('webAPI version and default project location (B4, B10)', () => {
  beforeEach(() => {
    resetAppVersionCache();
    vi.restoreAllMocks();
  });

  it('fetches the version from /health once and shares the cached promise across getAppVersion and getDebugInfo', async () => {
    const getSpy = vi.spyOn(apiClient, 'get').mockResolvedValue({
      success: true,
      data: { status: 'healthy', version: '3.21.1' },
    });

    const [version, debugInfo] = await Promise.all([
      webAPI.getAppVersion(),
      webAPI.getDebugInfo(),
    ]);

    expect(version).toBe('3.21.1');
    expect(debugInfo.systemInfo.appVersion).toBe('3.21.1');
    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(getSpy).toHaveBeenCalledWith('/health');
  });

  it('returns "unknown" when /health fails and retries on the next call', async () => {
    const getSpy = vi
      .spyOn(apiClient, 'get')
      .mockResolvedValueOnce({
        success: false,
        error: 'Service unavailable',
      })
      .mockResolvedValueOnce({
        success: true,
        data: { status: 'healthy', version: '3.21.2' },
      });

    const firstVersion = await webAPI.getAppVersion();
    expect(firstVersion).toBe('unknown');

    const secondVersion = await webAPI.getAppVersion();
    expect(secondVersion).toBe('3.21.2');
    expect(getSpy).toHaveBeenCalledTimes(2);
  });

  it('fetches GET /projects/default-location and returns path or null', async () => {
    const getSpy = vi
      .spyOn(apiClient, 'get')
      .mockResolvedValueOnce({
        success: true,
        data: { path: '/var/lib/aifactory/workspaces' },
      })
      .mockResolvedValueOnce({
        success: true,
        data: { path: null },
      });

    await expect(webAPI.getDefaultProjectLocation()).resolves.toBe(
      '/var/lib/aifactory/workspaces'
    );
    await expect(webAPI.getDefaultProjectLocation()).resolves.toBeNull();
    expect(getSpy).toHaveBeenNthCalledWith(1, '/projects/default-location');
    expect(getSpy).toHaveBeenNthCalledWith(2, '/projects/default-location');
  });
});
