import { describe, expect, it, vi } from 'vitest';
import { apiRequest } from './api-client';

const warn = vi.hoisted(() => vi.fn());
const error = vi.hoisted(() => vi.fn());
vi.mock('./logger', () => ({
  createLogger: () => ({ warn, error, debug: vi.fn() }),
}));

describe('apiRequest failure logs', () => {
  it('omits query URLs and response details from HTTP and network failure logs', async () => {
    const endpoint = '/settings/ollama/models?ollamaBaseUrl=http%3A%2F%2Fuser%3Apassword%40host';
    const secret = 'password';
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 400, statusText: 'Bad Request', json: async () => ({ detail: secret }) })
      .mockRejectedValueOnce(new Error(`Failed for ${endpoint}`)));

    await apiRequest(endpoint);
    await apiRequest(endpoint);

    expect(warn).toHaveBeenCalledWith('API error: GET /settings/ollama/models', { status: 400 });
    expect(error).toHaveBeenCalledWith('Network error: GET /settings/ollama/models');
    expect(JSON.stringify([...warn.mock.calls, ...error.mock.calls])).not.toContain(secret);
    vi.unstubAllGlobals();
  });
});
