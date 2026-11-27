import { API_BASE, get } from './api-client';
import { getAuthHeaders } from './auth';

export interface HumanQuestion {
  id: string;
  task_id: string;
  status: 'open' | 'expired' | 'answered' | 'rejected';
  question: string;
  choices: string[] | null;
  context: string;
  default: string | null;
  needs_approval: boolean;
  timeout_minutes: number;
  asked_at: string;
  expires_at: string;
  asked_by_model: string;
  answer: string | null;
  decision: 'approve' | 'reject' | null;
  note: string | null;
  answer_id: string | null;
  answered_by: string | null;
  channel: 'portal' | 'mcp' | 'cfactory' | null;
  auth: string | null;
  answered_at: string | null;
  delivery: null | 'answered' | 'inboxed' | 'launched';
}

export class QuestionRequestError extends Error {
  constructor(readonly status: number) { super(`HTTP ${status}`); }
}

const path = (taskId: string, qid?: string) =>
  `/tasks/${encodeURIComponent(taskId)}/questions${qid ? `/${encodeURIComponent(qid)}` : ''}`;

export async function getQuestions(taskId: string) {
  return get<{ task_id: string; review_reason: 'needs_input' | 'approval_rejected' | null; questions: HumanQuestion[] }>(path(taskId));
}

export async function answerQuestion(taskId: string, qid: string, answer: string) {
  const response = await fetch(`${API_BASE}${path(taskId, qid)}/answer`, {
    method: 'POST', credentials: 'include',
    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer }),
  });
  if (!response.ok) throw new QuestionRequestError(response.status);
  return response.json() as Promise<{ question: HumanQuestion }>;
}

// Approval requests must use the browser session cookie, even if a personal API key is stored.
async function cookieRequest<T>(url: string, body?: object): Promise<T> {
  const response = await fetch(`${API_BASE}${url}`, {
    method: body ? 'POST' : 'GET',
    credentials: 'include',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new QuestionRequestError(response.status);
  return response.json() as Promise<T>;
}

export async function decideQuestion(taskId: string, qid: string, decision: 'approve' | 'reject', note = '') {
  const base = path(taskId, qid);
  const { confirm_token } = await cookieRequest<{ confirm_token: string }>(`${base}/confirm-token`);
  if (!confirm_token) throw new Error('confirm');
  return cookieRequest<{ question: HumanQuestion }>(`${base}/answer`, { decision, note, confirm_token });
}
