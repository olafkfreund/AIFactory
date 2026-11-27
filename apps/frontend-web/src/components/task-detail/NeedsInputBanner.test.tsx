/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import fixture from '../../../../../tests/fixtures/questions_api.json';
import en from '../../shared/i18n/locales/en/tasks.json';
import { setAuthToken } from '../../lib/auth';
import { NeedsInputBanner } from './NeedsInputBanner';

vi.mock('react-i18next', () => {
  const t = (key: string, values?: Record<string, string>) => {
    const message = (en.needsInput as Record<string, string>)[key.replace(/^needsInput\./, '')];
    if (!message) return `UNKNOWN:${key}`;
    return message.replace(/{{(\w+)}}/g, (_, name: string) => values?.[name] ?? '');
  };
  return { useTranslation: () => ({ t }) };
});

const ok = (body: object) => ({ ok: true, status: 200, json: async () => body } as Response);
const failure = (status: number) => ({ ok: false, status, json: async () => ({ detail: 'server.text' }) } as Response);
const list = fixture.list.response.body;
const url = `/api/tasks/${list.task_id}/questions`;

beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); localStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

describe('NeedsInputBanner', () => {
  it('shows both open and the expired fixture questions, without answered or rejected ones', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(ok(list));
    const { container } = render(<NeedsInputBanner taskId={list.task_id} />);
    for (const q of list.questions.filter((item) => item.status === 'open' || item.status === 'expired')) {
      expect(await screen.findByText(q.question)).toBeInTheDocument();
    }
    for (const q of list.questions.filter((item) => item.status === 'answered' || item.status === 'rejected')) {
      expect(screen.queryByText(q.question)).not.toBeInTheDocument();
    }
    expect(screen.getByText('Suggested: UTC')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: en.needsInput.answer })).toHaveValue('');
    expect(container.querySelector('button[aria-pressed="true"], input[type="radio"]:checked')).toBeNull();
  });

  it.each([false, true])('renders nothing for an empty list (rejected=%s)', async (rejected) => {
    const emptyList = fixture.list_empty.response.body;
    const response = Promise.resolve(ok(emptyList));
    vi.mocked(fetch).mockReturnValueOnce(response);
    const { container } = render(<NeedsInputBanner taskId={emptyList.task_id} rejected={rejected} />);
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => { await response; });
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows an alert when loading fails', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(failure(500));
    render(<NeedsInputBanner taskId={list.task_id} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(en.needsInput.loadError);
  });

  it('renders question markup as literal text', async () => {
    const question = '<img src=x onerror=alert(1)><b>bold</b>';
    vi.mocked(fetch).mockResolvedValueOnce(ok({ ...list, questions: [{ ...list.questions[0], question }] }));
    const { container } = render(<NeedsInputBanner taskId={list.task_id} />);
    expect(await screen.findByText(question)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
  });

  it('sends a factual choice with the stored bearer token', async () => {
    setAuthToken('stored-token');
    vi.mocked(fetch).mockResolvedValueOnce(ok(list)).mockResolvedValueOnce(ok(fixture.answer_factual.response.body));
    render(<NeedsInputBanner taskId={list.task_id} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Local time' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch).toHaveBeenNthCalledWith(2, `${url}/${list.questions[0].id}/answer`, expect.objectContaining({
      headers: { Authorization: 'Bearer stored-token', 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: 'Local time' }),
    }));
  });

  it('uses only the cookie for approval confirmation and rejection with a note', async () => {
    setAuthToken('stored-token');
    const approval = list.questions.find((q) => q.needs_approval)!;
    vi.mocked(fetch).mockResolvedValueOnce(ok(list)).mockResolvedValueOnce(ok(fixture.confirm_token.response.body))
      .mockResolvedValueOnce(ok(fixture.answer_approval_reject.response.body));
    render(<NeedsInputBanner taskId={list.task_id} />);
    fireEvent.change(await screen.findByRole('textbox', { name: en.needsInput.note }), { target: { value: 'Keep the table' } });
    fireEvent.click(screen.getByRole('button', { name: en.needsInput.reject }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(fetch).toHaveBeenNthCalledWith(2, `${url}/${approval.id}/confirm-token`, expect.objectContaining({ credentials: 'include', headers: undefined }));
    expect(fetch).toHaveBeenNthCalledWith(3, `${url}/${approval.id}/answer`, expect.objectContaining({
      credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision: 'reject', note: 'Keep the table', confirm_token: fixture.confirm_token.response.body.confirm_token }),
    }));
    expect(new Headers(vi.mocked(fetch).mock.calls[1][1]?.headers).has('Authorization')).toBe(false);
    expect(new Headers(vi.mocked(fetch).mock.calls[2][1]?.headers).has('Authorization')).toBe(false);
  });

  it('translates a 409 and refetches the list', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(ok(list)).mockResolvedValueOnce(failure(409)).mockResolvedValueOnce(ok(list));
    render(<NeedsInputBanner taskId={list.task_id} />);
    fireEvent.click(await screen.findByRole('button', { name: 'UTC' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(en.needsInput.alreadyAnswered);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(fetch).toHaveBeenNthCalledWith(3, url, expect.anything());
  });

  it('shows the portal sign-in message for a 403 on approval', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(ok(list)).mockResolvedValueOnce(failure(403));
    render(<NeedsInputBanner taskId={list.task_id} />);
    fireEvent.click(await screen.findByRole('button', { name: en.needsInput.approve }));
    expect(await screen.findByRole('alert')).toHaveTextContent(en.needsInput.approvalSignIn);
    expect(screen.queryByText('server.text')).not.toBeInTheDocument();
  });

  it('shows the rejected question and note read-only', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(ok(list));
    render(<NeedsInputBanner taskId={list.task_id} rejected />);
    const rejected = list.questions.find((q) => q.status === 'rejected')!;
    expect(await screen.findByText(rejected.question)).toBeInTheDocument();
    expect(screen.getByText(`Note: ${rejected.note}`)).toBeInTheDocument();
    expect(screen.getByText(en.needsInput.rejectedNotice)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start|resume/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
});
