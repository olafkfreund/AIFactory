import { get, post } from './api-client';

export interface HumanQuestion {
  id: string;
  question: string;
  choices?: string[];
  context?: string;
  default?: string;
  needs_approval: boolean;
  status: string;
}

const path = (taskId: string, qid?: string) =>
  `/tasks/${encodeURIComponent(taskId)}/questions${qid ? `/${encodeURIComponent(qid)}` : ''}`;

export async function getQuestions(taskId: string) {
  return get<{ questions: HumanQuestion[] }>(path(taskId));
}

export async function answerQuestion(taskId: string, qid: string, answer: string) {
  return post(path(taskId, qid) + '/answer', { answer });
}

// Approval requests must use the browser session cookie, even if a personal API key is stored.
async function cookieRequest(url: string, body?: object): Promise<{ confirm_token?: string }> {
  const response = await fetch(`${import.meta.env.VITE_API_BASE_URL || '/api'}${url}`, {
    method: body ? 'POST' : 'GET',
    credentials: 'include',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
  return data;
}

export async function decideQuestion(taskId: string, qid: string, decision: 'approve' | 'reject', note = '') {
  const base = path(taskId, qid);
  const { confirm_token } = await cookieRequest(`${base}/confirm-token`);
  if (!confirm_token) throw new Error('needsInput.confirmError');
  return cookieRequest(`${base}/answer`, { decision, note, confirm_token });
}
