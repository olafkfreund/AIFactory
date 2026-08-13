// Single-provider benchmark runner: create the SAME task with one provider's
// model, poll to a terminal state, and emit time + result metrics as JSON.
// Run once per provider (sequentially). Cost is captured best-effort elsewhere.
//
//   DEMO_MODEL=sonnet DEMO_PROVIDER=claude node scripts/benchmark-provider.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const API = process.env.AIFACTORY_API_URL || 'http://localhost:3101';
const PID = process.env.AIFACTORY_PROJECT_ID || 'f7ac8d99-b913-4c6f-afce-f8376e29c98c';
const TOKEN = fs.readFileSync(path.join(os.homedir(), '.aifactory', '.token'), 'utf8').trim();
const MODEL = process.env.DEMO_MODEL || 'sonnet';
const PROVIDER = process.env.DEMO_PROVIDER || 'claude';
const DEADLINE_MIN = +(process.env.DEMO_DEADLINE_MIN || 25);
// A fixed /tmp path lets any local user pre-create it as a symlink and redirect
// (or read) the result file — CodeQL js/insecure-temporary-file. mkdtempSync is
// the atomic, 0700, unpredictable equivalent. Set OUT to pick your own dir.
const OUT = process.env.OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'aifactory-bench-'));
const REPO = '/tmp/aifactory-demo';
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const TITLE = 'Add Tic-Tac-Toe game';
const DESC = [
  'Add a Tic-Tac-Toe game to the FastAPI app.',
  '1. Create src/app/tictactoe.py with a TicTacToe class: a 3x3 board, a move(row, col, player) method,',
  '   win detection (rows, columns, both diagonals), draw detection, and rejection of out-of-range or',
  '   already-taken moves (raise ValueError).',
  '2. Expose POST /tictactoe/move accepting JSON {board: 3x3 list, row, col, player} and returning',
  '   {board, winner, draw} with HTTP 200; invalid moves return HTTP 400.',
  '3. Add tests/test_tictactoe.py (pytest + fastapi.testclient.TestClient) covering: a winning line,',
  '   a draw, and a rejected invalid move.',
].join('\n');

async function api(p, method = 'GET', body) {
  const r = await fetch(`${API}${p}`, {
    method,
    headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const t = await r.text();
  try { return { status: r.status, json: JSON.parse(t) }; } catch { return { status: r.status, text: t }; }
}

(async () => {
  log(`benchmark ${PROVIDER} (${MODEL})`);
  const t0 = Date.now();
  const created = await api(`/api/projects/${PID}/tasks`, 'POST', {
    title: TITLE, description: DESC,
    metadata: { model: MODEL, requireReviewBeforeCoding: false },
  });
  const taskId = created.json?.id;
  const specId = created.json?.specId;
  log('created', taskId, 'http', created.status);
  if (!taskId) { log('CREATE FAILED', created.text || created.json); process.exit(1); }
  await api(`/api/tasks/${taskId}/start`, 'POST', {});

  const deadline = t0 + DEADLINE_MIN * 60 * 1000;
  let last = '', terminal = null, detail = {};
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 8000));
    detail = (await api(`/api/tasks/${taskId}`)).json || {};
    const subs = detail.subtasks || [];
    const done = subs.filter(s => ['completed', 'done'].includes((s.status || '').toLowerCase())).length;
    const failed = subs.filter(s => (s.status || '').toLowerCase() === 'failed').length;
    const key = `${detail.status}/${detail.phase}/${detail.reviewReason}/${done}+${failed}/${subs.length}`;
    if (key !== last) { log('STATE', key); last = key; }
    const st = (detail.status || '').toLowerCase();
    if (st === 'human_review' || st === 'done' || st === 'completed') { terminal = st; break; }
  }
  const t1 = Date.now();
  const durationS = Math.round((t1 - t0) / 1000);
  log(`finished: ${terminal || 'TIMEOUT'} in ${durationS}s`);

  // result artifacts
  const subs = detail.subtasks || [];
  const doneN = subs.filter(s => ['completed', 'done'].includes((s.status || '').toLowerCase())).length;
  let diffstat = '';
  try {
    // execFileSync, not execSync: specId comes back from the API, and in a
    // shell string it is a command, not an argument. No shell also means no
    // `2>/dev/null`, so stderr is simply not captured.
    diffstat = execFileSync(
      'git',
      ['-C', REPO, 'diff', '--stat', `main..aifactory/${specId}`, '--', 'src', 'tests'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {}
  const result = {
    provider: PROVIDER,
    model: MODEL,
    spec_id: specId,
    task_id: taskId,
    terminal_state: terminal || 'timeout',
    review_reason: detail.reviewReason || null,
    duration_seconds: durationS,
    subtasks_done: doneN,
    subtasks_total: subs.length,
    diff_stat: diffstat,
    produced_code: /tictactoe|src\/app|tests/.test(diffstat),
  };
  const outFile = path.join(OUT, `${PROVIDER}.json`);
  fs.writeFileSync(outFile, JSON.stringify(result, null, 2));
  log('RESULT', JSON.stringify(result));
  log('wrote', outFile);
})().catch(e => { console.error('BENCH_ERR', e.stack || e.message); process.exit(1); });
