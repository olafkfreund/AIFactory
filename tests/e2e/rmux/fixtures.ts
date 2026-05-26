/**
 * Shared fixtures for the rmux E2E scenarios.
 *
 * All three scenarios start from the same precondition: a project
 * exists, a task has been started, and the rmux session is alive in
 * the daemon.  This file builds that state via the REST API so each
 * spec stays focused on its specific assertion.
 */

import { test as base, expect, APIRequestContext } from '@playwright/test';

const API = process.env.AIFACTORY_E2E_API ?? 'http://localhost:3101';
const TOKEN = process.env.AIFACTORY_E2E_TOKEN ?? '';
const PROJECT_ID = process.env.AIFACTORY_E2E_PROJECT_ID ?? '';

if (!TOKEN || !PROJECT_ID) {
  console.warn(
    '[e2e] AIFACTORY_E2E_TOKEN and AIFACTORY_E2E_PROJECT_ID must be set'
  );
}

const authHeaders = { Authorization: `Bearer ${TOKEN}` };

/**
 * Snapshot of a live task we created for the test scope.  The fixture
 * tears it down (recover → backlog) on teardown so reruns are idempotent.
 */
export interface ActiveTask {
  specId: string;
  taskId: string; // composite project:spec
}

export const test = base.extend<{
  activeTask: ActiveTask;
}>({
  /**
   * Imports the next available GitHub issue from aifactory-test and
   * starts it.  Yields once the rmux session for the spec is live
   * (verified via POST /attach probe — 200 or 409 both prove it).
   * Teardown: recover the task back to backlog so we don't leak state.
   */
  activeTask: async ({ request }, use) => {
    // 1. Find the lowest-numbered backlog task that hasn't been started.
    const listRes = await request.get(`${API}/api/projects/${PROJECT_ID}/tasks`, {
      headers: authHeaders,
    });
    expect(listRes.status()).toBe(200);
    const tasks: any[] = await listRes.json();
    const backlog = tasks
      .filter((t) => t.status === 'backlog')
      .sort((a, b) => a.specId.localeCompare(b.specId));
    if (backlog.length === 0) {
      throw new Error('[e2e] no backlog task available — reset some via /recover');
    }
    const task = backlog[0];
    const specId = task.specId;
    const taskId = task.id;

    // 2. Recover with autoRestart → fires agent_service.start_task_execution
    //    → fires the rmux create hook.
    const recoverRes = await request.post(
      `${API}/api/tasks/${encodeURIComponent(taskId)}/recover`,
      {
        headers: { ...authHeaders, 'Content-Type': 'application/json' },
        data: { targetStatus: 'backlog', autoRestart: true },
      }
    );
    expect(recoverRes.status()).toBe(200);

    // 3. Poll the attach endpoint until it stops 404'ing — that proves
    //    the rmux session is registered AND we can reach the bridge.
    await expect
      .poll(
        async () => {
          const r = await request.post(
            `${API}/api/tasks/${encodeURIComponent(specId)}/agent-console/attach`,
            {
              headers: { ...authHeaders, 'Content-Type': 'application/json' },
              data: { connection_id: 'fixture-probe' },
            }
          );
          return r.status();
        },
        {
          timeout: 60_000,
          message: 'rmux session for spec never appeared in registry',
        }
      )
      .toBeOneOf([200, 409]);

    // Release the fixture's probe attach so the actual test can claim
    // its own connection_id.  Ignore errors — could already be detached
    // by the WS-disconnect path.
    await request
      .post(
        `${API}/api/tasks/${encodeURIComponent(specId)}/agent-console/detach`,
        {
          headers: { ...authHeaders, 'Content-Type': 'application/json' },
          data: { connection_id: 'fixture-probe' },
        }
      )
      .catch(() => {});

    await use({ specId, taskId });

    // ---- Teardown: stop the task and recover it to backlog ----
    await request
      .post(`${API}/api/tasks/${encodeURIComponent(taskId)}/stop`, {
        headers: { ...authHeaders, 'Content-Type': 'application/json' },
        data: {},
      })
      .catch(() => {});
  },
});

export { expect };

/** Helper: read the AuditLog table via the API.  Used by Scenario 3. */
export async function fetchAuditLog(
  request: APIRequestContext,
  action: string
): Promise<any[]> {
  const r = await request.get(
    `${API}/api/orgs/_/audit-logs?action=${encodeURIComponent(action)}&limit=10`,
    { headers: authHeaders }
  );
  if (r.status() !== 200) return [];
  const body = await r.json();
  return body.data ?? body ?? [];
}
