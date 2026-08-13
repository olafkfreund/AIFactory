// "Finish the PR" screencast: a task already at the Human Review gate (AIFactory
// done) → developer clicks Create PR → a real GitHub PR is opened. Records mp4.
//
//   node scripts/demo-capture-finishpr.mjs <title-fragment> <out-dir>
import { chromium } from '@playwright/test';
import { execFileSync, execSync } from 'node:child_process';
import { assertNotOption, findFilesUnder } from './argv-safety.cjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const FRAG = process.argv[2] || 'echo';
const OUT = process.argv[3] || '/tmp/aifactory-finishpr';
const PORTAL = process.env.AIFACTORY_PORTAL_URL || 'http://localhost:3100';
const TOKEN = fs.readFileSync(path.join(os.homedir(), '.aifactory', '.token'), 'utf8').trim();
fs.mkdirSync(OUT, { recursive: true });
const VIDDIR = path.join(OUT, 'video'); fs.mkdirSync(VIDDIR, { recursive: true });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

function findChrome() {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || '';
  for (const v of ['chromium-1217', 'chromium-1223', 'chromium-1208']) {
    const p = path.join(root, v, 'chrome-linux64', 'chrome'); if (fs.existsSync(p)) return p;
  }
  // argv array -- $PLAYWRIGHT_BROWSERS_PATH must not reach a shell.
  try { return findFilesUnder(root, 'chrome') || undefined; } catch { return undefined; }
}
// Constant command, no interpolation; `command` is a shell builtin.
function ffmpeg() { try { return execSync('command -v ffmpeg', { encoding: 'utf8' }).trim() || 'ffmpeg'; } catch { return 'ffmpeg'; } }

let n = 0;
async function shot(page, name) { const f = path.join(OUT, `${String(++n).padStart(2,'0')}-${name}.png`); await page.screenshot({ path: f }); log('SHOT', path.basename(f)); }

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: findChrome() });
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    extraHTTPHeaders: { Authorization: `Bearer ${TOKEN}` },
    recordVideo: { dir: VIDDIR, size: { width: 1600, height: 900 } },
  });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  await page.goto(PORTAL, { waitUntil: 'networkidle' }); await page.waitForTimeout(1500);

  // login
  for (let i = 1; i <= 4; i++) {
    const input = page.getByPlaceholder(/token/i).first();
    let on = false; try { on = await input.isVisible({ timeout: 4000 }); } catch {}
    if (!on) break;
    await input.fill(TOKEN);
    await page.getByRole('button', { name: /^login$|continue|sign in/i }).first().click({ timeout: 4000 }).catch(() => input.press('Enter'));
    try { await page.getByText(/Human Review|In Progress|Backlog/i).first().waitFor({ state: 'visible', timeout: 6000 }); log('logged in', i); break; } catch {}
  }
  await page.waitForTimeout(3000); // dwell on the board (AIFactory done → Human Review)
  await shot(page, 'board');

  // open the task at the gate
  await page.getByText(FRAG, { exact: false }).first().click({ timeout: 8000 });
  await page.waitForTimeout(4000); // dwell on the "Build Ready for Review" gate
  await shot(page, 'review-gate');

  // click the gate "Create PR" button (not the dialog's "Create Pull Request")
  log('clicking Create PR');
  await page.getByRole('button', { name: /^create pr$/i }).first().click({ timeout: 8000 });
  await page.waitForTimeout(1000);
  // dialog — wait for fork detection to finish and the submit button to enable
  const submit = page.getByRole('button', { name: /^create pull request$/i }).first();
  try { await submit.waitFor({ state: 'visible', timeout: 8000 }); } catch {}
  let enabled = false;
  for (let i = 0; i < 24; i++) { if (await submit.isEnabled().catch(() => false)) { enabled = true; break; } await page.waitForTimeout(500); }
  log('submit enabled:', enabled);
  await page.waitForTimeout(3000); // dwell on the populated Create-PR dialog
  await shot(page, 'create-pr-dialog');

  // submit the PR (real push + gh pr create) and confirm the click registered
  log('submitting PR (real create on aifactory-demo)');
  await submit.click({ timeout: 8000 });
  try { await page.getByText(/Creating PR|Pull Request Created|PR #\d+/i).first().waitFor({ state: 'visible', timeout: 10000 }); log('submit registered'); }
  catch { log('submit did not register — retrying'); await submit.click({ timeout: 5000 }).catch(() => {}); }

  // wait for success ("Pull Request Created" / "PR #") or failure, up to 90s
  let outcome = 'unknown';
  for (let i = 0; i < 30; i++) {
    await page.waitForTimeout(3000);
    const body = await page.textContent('body').catch(() => '') || '';
    if (/Pull Request Created|PR #\d+|View PR|Merge PR/i.test(body)) { outcome = 'created'; break; }
    if (/Failed to create PR|forkDetectionFailed|error/i.test(body) && i > 2) { outcome = 'maybe-error'; }
    if (i === 2 || i === 6) await shot(page, 'creating');
  }
  log('PR outcome:', outcome);
  await page.waitForTimeout(1500);
  await shot(page, 'pr-created');
  await page.waitForTimeout(5000); // dwell on the "PR #N created" success toast

  await context.close(); await browser.close();

  // webm -> mp4 (real speed, this clip is short)
  const webm = fs.readdirSync(VIDDIR).filter(f => f.endsWith('.webm')).map(f => path.join(VIDDIR, f))[0];
  if (webm) {
    const out = path.join(OUT, 'finish-pr.mp4');
    try {
      execFileSync(assertNotOption(ffmpeg(), 'ffmpeg path'), [
        '-y', '-i', webm,
        '-vf', 'scale=1280:-2',
        '-r', '24', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-an',
        out,
      ], { stdio: 'ignore' });
      log('VIDEO', out);
    } catch (e) { log('transcode failed', e.message); log('WEBM', webm); }
  }
  log('DONE');
})().catch(e => { console.error('CAPTURE_ERR', e.stack || e.message); process.exit(1); });
