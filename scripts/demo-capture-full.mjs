// Full-lifecycle portal screencast: records an empty board, creates+starts a
// task live, demos the in-browser terminal, then watches the task through every
// column/gate to the review gate. Produces a webm + milestone PNGs.
//
//   node scripts/demo-capture-full.mjs <mode> <out-dir>
//     mode = terminal-test : login -> board -> terminal demo (quick validation)
//     mode = full          : whole lifecycle (long; run in background)
import { chromium } from '@playwright/test';
import { execSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const MODE = process.argv[2] || 'full';
const OUT = process.argv[3] || '/tmp/aifactory-demo-full';
const PORTAL = process.env.AIFACTORY_PORTAL_URL || 'http://localhost:3100';
const API = process.env.AIFACTORY_API_URL || 'http://localhost:3101';
const PID = process.env.AIFACTORY_PROJECT_ID || 'f7ac8d99-b913-4c6f-afce-f8376e29c98c';
const TOKEN = fs.readFileSync(path.join(os.homedir(), '.aifactory', '.token'), 'utf8').trim();
const TITLE = process.env.DEMO_TITLE || 'Add /status endpoint';
const DESC = process.env.DEMO_DESC || 'Add a GET /status endpoint to the FastAPI app that returns {"uptime_seconds": <float>} measured from process start, plus a pytest test using TestClient.';

fs.mkdirSync(OUT, { recursive: true });
const VIDDIR = path.join(OUT, 'video');
fs.mkdirSync(VIDDIR, { recursive: true });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

function findChrome() {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || '';
  for (const v of ['chromium-1217', 'chromium-1223', 'chromium-1208']) {
    const p = path.join(root, v, 'chrome-linux64', 'chrome');
    if (fs.existsSync(p)) return p;
  }
  try { return execSync(`find -L ${root} -maxdepth 3 -type f -name chrome 2>/dev/null | head -1`).toString().trim(); } catch { return undefined; }
}

function ffmpegBin() {
  try { const p = execSync('command -v ffmpeg').toString().trim(); if (p) return p; } catch {}
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || '';
  try { const p = execSync(`find -L ${root} -maxdepth 3 -type f -name 'ffmpeg*' 2>/dev/null | head -1`).toString().trim(); if (p) return p; } catch {}
  return 'ffmpeg';
}

function probeDuration(ff, f) {
  try {
    const o = execSync(`'${ff}' -i '${f}' 2>&1 | grep Duration | head -1`).toString();
    const m = o.match(/Duration: (\d+):(\d+):(\d+\.\d+)/);
    if (m) return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
  } catch {}
  return 0;
}

// Variable-speed render: intro+terminal at 2×, the long build middle squeezed to
// ~22s, the review gate at 1×. Boundaries come from real run timestamps.
function transcode(webm, outMp4, introEnd, gateStart) {
  const ff = ffmpegBin();
  const dur = probeDuration(ff, webm);
  let a = introEnd > 2 ? introEnd : Math.min(25, dur * 0.1);
  let g = gateStart > a ? gateStart : Math.max(a + 5, dur - 15);
  a = Math.min(a, Math.max(1, dur - 2));
  g = Math.min(g, Math.max(a + 1, dur - 1));
  const midMul = Math.min(0.5, Math.max(0.02, 22 / Math.max(1, g - a))); // → ~22s middle
  const filter =
    `[0:v]trim=0:${a.toFixed(2)},setpts=(PTS-STARTPTS)*0.5[s0];` +
    `[0:v]trim=${a.toFixed(2)}:${g.toFixed(2)},setpts=(PTS-STARTPTS)*${midMul.toFixed(4)}[s1];` +
    `[0:v]trim=${g.toFixed(2)},setpts=(PTS-STARTPTS)[s2];` +
    `[s0][s1][s2]concat=n=3:v=1[m];[m]scale=1280:-2[outv]`;
  execSync(`'${ff}' -y -i '${webm}' -filter_complex "${filter}" -map "[outv]" -r 24 -pix_fmt yuv420p -movflags +faststart -an '${outMp4}'`, { stdio: 'ignore' });
  return outMp4;
}

async function api(p, method = 'GET', body) {
  const r = await fetch(`${API}${p}`, {
    method,
    headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const t = await r.text();
  try { return { status: r.status, json: JSON.parse(t) }; } catch { return { status: r.status, json: null, text: t }; }
}

let n = 0;
async function shot(page, name) {
  const f = path.join(OUT, `${String(++n).padStart(2, '0')}-${name}.png`);
  await page.screenshot({ path: f });
  log('SHOT', path.basename(f));
}

async function clickText(page, txt, timeout = 3000) {
  try { await page.getByText(txt, { exact: false }).first().click({ timeout }); return true; } catch { return false; }
}

async function ensureLoggedIn(page) {
  for (let i = 1; i <= 4; i++) {
    const input = page.getByPlaceholder(/token/i).first();
    let on = false; try { on = await input.isVisible({ timeout: 4000 }); } catch {}
    if (!on) return true;
    await input.fill(TOKEN);
    await page.getByRole('button', { name: /^login$|continue|sign in/i }).first().click({ timeout: 4000 }).catch(() => input.press('Enter'));
    try { await page.getByText(/In Progress|Human Review|Backlog/i).first().waitFor({ state: 'visible', timeout: 6000 }); log('logged in', i); return true; }
    catch { await page.waitForTimeout(1500); }
  }
  return false;
}

async function terminalDemo(page) {
  // Open the in-browser terminal (sidebar "Terminal(s)") and run a few commands.
  log('terminal demo: opening Terminal view');
  if (!await clickText(page, 'Terminal', 2500)) await clickText(page, 'Terminals', 2500);
  await page.waitForTimeout(2000);
  await shot(page, 'terminal-open');

  // Create a terminal: prefer the button, fall back to the Ctrl+Shift+E shortcut.
  const xterm = page.locator('.xterm').first();
  let haveTerm = await xterm.count().then(c => c > 0).catch(() => false);
  if (!haveTerm) {
    try {
      await page.getByRole('button', { name: /new terminal/i }).first().click({ timeout: 3000 });
    } catch {
      await page.keyboard.press('Control+Shift+E');
    }
    try { await xterm.waitFor({ state: 'visible', timeout: 8000 }); haveTerm = true; }
    catch { log('no xterm appeared'); }
  }
  await page.waitForTimeout(1500);

  if (haveTerm) {
    await xterm.click({ timeout: 3000 }).catch(() => page.mouse.click(800, 450));
    await page.waitForTimeout(700);
    for (const cmd of ['cd /tmp/aifactory-demo', 'ls', 'git log --oneline -5', 'cat src/app/main.py']) {
      await page.keyboard.type(cmd, { delay: 55 });
      await page.keyboard.press('Enter');
      await page.waitForTimeout(1400);
    }
    await page.waitForTimeout(1500);
  }
  await shot(page, 'terminal-commands');
  log('terminal demo done (xterm=' + haveTerm + ')');
}

(async () => {
  const exe = findChrome();
  log('chromium', exe, 'mode', MODE);
  const browser = await chromium.launch({ headless: true, executablePath: exe });
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    extraHTTPHeaders: { Authorization: `Bearer ${TOKEN}` },
    recordVideo: { dir: VIDDIR, size: { width: 1600, height: 900 } },
  });
  const tStart = Date.now();           // recording timeline origin (context created)
  let introEnd = 0, gateStart = 0;     // seconds-from-start of the two cut points
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  await page.goto(PORTAL, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await ensureLoggedIn(page);
  await page.waitForTimeout(1500);
  await shot(page, 'board-empty');

  if (MODE === 'terminal-test') {
    await terminalDemo(page);
    await context.close(); await browser.close();
    log('terminal-test done'); return;
  }

  // --- FULL lifecycle ---
  // 1. Create + start the task via API; the board updates live over WS.
  log('creating task');
  const created = await api(`/api/projects/${PID}/tasks`, 'POST', { title: TITLE, description: DESC });
  const taskId = created.json?.id;
  log('created', taskId, 'status', created.status);
  await api(`/api/tasks/${taskId}/start`, 'POST', {});
  // The board live-pushes new cards inconsistently in a headless session, so
  // reload to make the freshly-added card visibly pop in for this beat.
  await page.waitForTimeout(1500);
  await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
  try { await page.getByText(TITLE.replace('Add ', ''), { exact: false }).first().waitFor({ state: 'visible', timeout: 8000 }); } catch {}
  await page.waitForTimeout(1500);
  await shot(page, 'task-added-started');

  // 2. Terminal demo while the agent spins up.
  await terminalDemo(page);

  // 3. Watch through the columns/gates.
  await clickText(page, 'Tasks', 2000); // back to board
  await page.waitForTimeout(1500);
  introEnd = (Date.now() - tStart) / 1000; // end of the readable intro/terminal segment
  const seen = new Set();
  const deadline = Date.now() + 13 * 60 * 1000;
  let last = '';
  while (Date.now() < deadline) {
    const d = (await api(`/api/tasks/${taskId}`)).json || {};
    const status = d.status, phase = d.phase, review = d.reviewReason;
    const done = (d.subtasks || []).filter(s => ['completed', 'done'].includes((s.status || '').toLowerCase())).length;
    const tot = (d.subtasks || []).length;
    const key = `${status}/${phase}/${review}/${done}/${tot}`;
    if (key !== last) { log('STATE', key); last = key; }

    // Refresh board so the card's column move is on camera.
    await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(2500);

    // Milestone screenshots (once each).
    if (status === 'in_progress' && !seen.has('coding')) { seen.add('coding'); await shot(page, 'board-in-progress');
      // peek at live progress detail
      if (await clickText(page, TITLE.replace('Add ', ''), 3000)) { await page.waitForTimeout(1500); await shot(page, 'detail-coding');
        if (await page.getByRole('tab', { name: /logs/i }).first().click({ timeout: 2000 }).then(()=>true).catch(()=>false)) { await page.waitForTimeout(1500); await shot(page, 'detail-logs'); }
        await page.keyboard.press('Escape').catch(()=>{}); await page.waitForTimeout(800);
      }
    }
    if (status === 'human_review') {
      gateStart = (Date.now() - tStart) / 1000; // build done; gate begins here
      await shot(page, 'board-human-review');
      if (await clickText(page, TITLE.replace('Add ', ''), 3000)) { await page.waitForTimeout(2000); await shot(page, 'detail-review-gate'); }
      log('GATE reached'); break;
    }
    if (['done', 'completed'].includes((status || '').toLowerCase())) { await shot(page, 'board-done'); break; }
    await page.waitForTimeout(5000);
  }

  await page.waitForTimeout(1500);
  await context.close();
  await browser.close();

  // Transcode + variable-speed the recording into a watchable mp4.
  const webm = fs.readdirSync(VIDDIR).filter(f => f.endsWith('.webm')).map(f => path.join(VIDDIR, f))[0];
  if (webm) {
    const out = path.join(OUT, 'lifecycle.mp4');
    log(`transcoding (introEnd=${introEnd.toFixed(0)}s gateStart=${gateStart.toFixed(0)}s)`);
    try { transcode(webm, out, introEnd, gateStart); log('VIDEO', out); }
    catch (e) { log('transcode failed:', e.message); log('WEBM', webm); }
  }
  log('DONE');
})().catch(e => { console.error('CAPTURE_ERR', e.stack || e.message); process.exit(1); });
