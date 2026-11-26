/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { NeedsInputBanner } from './NeedsInputBanner';

vi.mock('react-i18next', () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});

const response = (body: object) => ({ ok: true, json: async () => body } as Response);

beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); });
afterEach(() => { vi.unstubAllGlobals(); });

describe('NeedsInputBanner', () => {
  it('sends a selected choice', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response({ questions: [{ id: 'q1', question: 'Which?', choices: ['A', 'B'], needs_approval: false, status: 'open' }] }))
      .mockResolvedValueOnce(response({}));
    render(<NeedsInputBanner taskId="task:1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'B' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch).toHaveBeenLastCalledWith('/api/tasks/task%3A1/questions/q1/answer', expect.objectContaining({ body: JSON.stringify({ answer: 'B' }) }));
  });

  it('sends free text', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response({ questions: [{ id: 'q2', question: 'Explain?', needs_approval: false, status: 'expired' }] }))
      .mockResolvedValueOnce(response({}));
    render(<NeedsInputBanner taskId="task1" />);
    fireEvent.change(await screen.findByRole('textbox', { name: 'needsInput.answer' }), { target: { value: '  because  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'needsInput.send' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch).toHaveBeenLastCalledWith('/api/tasks/task1/questions/q2/answer', expect.objectContaining({ body: JSON.stringify({ answer: 'because' }) }));
  });

  it('fetches a token before approval without an Authorization header', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response({ questions: [{ id: 'q3', question: 'Ship?', needs_approval: true, status: 'open' }] }))
      .mockResolvedValueOnce(response({ confirm_token: 'one-use' }))
      .mockResolvedValueOnce(response({}));
    render(<NeedsInputBanner taskId="task1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'needsInput.approve' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/tasks/task1/questions/q3/confirm-token', expect.objectContaining({ credentials: 'include', headers: undefined }));
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/tasks/task1/questions/q3/answer', expect.objectContaining({ credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision: 'approve', note: '', confirm_token: 'one-use' }) }));
  });
});
