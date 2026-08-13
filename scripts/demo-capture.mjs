// Demo capture: screenshots + screencast of the AIFactory portal showing a task.
// Headless Playwright against the NixOS-patched chromium. Run:
//   node scripts/demo-capture.mjs "<task title fragment>" /out/dir
import { chromium } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { assertNotOption, findFilesUnder } from './argv-safety.cjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const FRAG = process.argv[2] || 'ping';
const OUT = process.argv[3] || '/tmp/aifactory-demo-shots';
const PORTAL = process.env.AIFACTORY_PORTAL_URL || 'http://localhost:3100';
const TOKEN = fs.readFileSync(path.join(os.homedir(), '.aifactory', '.token'), 'utf8').trim();

fs.mkdirSync(OUT, { recursive: true });
const VIDDIR = path.join(OUT, 'video');
fs.mkdirSync(VIDDIR, { recursive: true });

// Find the NixOS-patched chromium binary (headless-shell isn't present, so we
// point executablePath at the full chrome, which runs on NixOS).
function findChrome() {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || '';
  for (const ver of ['chromium-1217', 'chromium-1223', 'chromium-1208']) {
    const p = path.join(root, ver, 'chrome-linux64', 'chrome');
    if (fs.existsSync(p)) return p;
  }
  // fallback: search (argv array -- $PLAYWRIGHT_BROWSERS_PATH must not reach a shell)
  try {
    return findFilesUnder(root, 'chrome') || undefined;
  } catch { return undefined; }
}

const shots = [];
async function shot(page, name) {
  const f = path.join(OUT, `${String(shots.length + 1).padStart(2, '0')}-${name}.png`);
  await page.screenshot({ path: f, fullPage: false });
  shots.push(f);
  console.log('SHOT', f);
}

async function clickText(page, txt, timeout = 4000) {
  try {
    const el = page.getByText(txt, { exact: false }).first();
    await el.click({ timeout });
    return true;
  } catch { return false; }
}

const log = (...a) => console.log(...a);

(async () => {
  const exe = findChrome();
  log('chromium:', exe);
  const browser = await chromium.launch({ headless: true, executablePath: exe });
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    extraHTTPHeaders: { Authorization: `Bearer ${TOKEN}` },
    recordVideo: { dir: VIDDIR, size: { width: 1600, height: 900 } },
    storageState: {
      cookies: [],
      origins: [{
        origin: PORTAL,
        localStorage: [
          { name: 'aifactory.token', value: TOKEN },
          { name: 'auth_token', value: TOKEN },
          { name: 'aifactory.gitDialogSkipped', value: 'true' },
        ],
      }],
    },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);

  await page.goto(PORTAL, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // The SPA has its own token gate even when the backend has DISABLE_AUTH.
  // Log in via the form, retrying until we actually leave the login screen.
  async function ensureLoggedIn() {
    for (let attempt = 1; attempt <= 4; attempt++) {
      const input = page.getByPlaceholder(/token/i).first();
      let onLogin = false;
      try { onLogin = await input.isVisible({ timeout: 4000 }); } catch {}
      if (!onLogin) { log('authed (no login form)'); return true; }
      await input.fill(TOKEN);
      const btn = page.getByRole('button', { name: /^login$|continue|sign in/i }).first();
      await btn.click({ timeout: 4000 }).catch(() => input.press('Enter'));
      // Wait for a board landmark to confirm we're through.
      try {
        await page.getByText(/In Progress|Human Review|Backlog/i).first()
          .waitFor({ state: 'visible', timeout: 6000 });
        log('logged in via form (attempt ' + attempt + ')');
        return true;
      } catch { log('login attempt ' + attempt + ' not through yet'); await page.waitForTimeout(1500); }
    }
    return false;
  }
  await ensureLoggedIn();
  await page.waitForTimeout(1200);
  await shot(page, 'landing');

  // Dismiss any blocking dialog (git-required / onboarding).
  for (const t of ['Skip for now', 'Skip', 'Got it', 'Continue']) {
    if (await clickText(page, t, 1500)) { await page.waitForTimeout(800); break; }
  }

  // Ensure a project is selected: click a project entry mentioning demo if a
  // picker/list is shown.
  for (const t of ['aifactory-demo', 'AIFACTORY-DEMO']) {
    if (await clickText(page, t, 1500)) { await page.waitForTimeout(1200); break; }
  }
  await page.waitForTimeout(1200);
  await shot(page, 'board');

  // Open the task by its title fragment.
  let opened = await clickText(page, FRAG, 4000);
  if (!opened) opened = await clickText(page, '/' + FRAG, 4000);
  await page.waitForTimeout(1500);
  await shot(page, 'task-detail');

  // Tour the real detail tabs (Overview is the default landing view; the
  // modal footer carries the review actions: Merge to main / Create PR /
  // Request Changes). Use role=tab so we don't accidentally match body text.
  for (const tab of ['Subtasks', 'Files', 'Logs']) {
    try {
      await page.getByRole('tab', { name: new RegExp(tab, 'i') }).first().click({ timeout: 2500 });
      await page.waitForTimeout(1200);
      await shot(page, 'tab-' + tab.toLowerCase());
    } catch { /* tab absent */ }
  }

  // Zoom the review action bar (the merge/PR/request-changes buttons).
  try {
    const merge = page.getByRole('button', { name: /merge to main/i }).first();
    await merge.scrollIntoViewIfNeeded({ timeout: 2000 });
    await page.waitForTimeout(500);
    await shot(page, 'review-actions');
  } catch { /* not at review gate */ }

  await page.waitForTimeout(800);
  await context.close(); // finalizes the video
  await browser.close();

  // Locate the produced webm and convert to mp4 if ffmpeg is available.
  const webm = fs.readdirSync(VIDDIR).filter(f => f.endsWith('.webm')).map(f => path.join(VIDDIR, f))[0];
  let mp4 = '';
  if (webm) {
    mp4 = path.join(OUT, 'demo-screencast.mp4');
    const root = process.env.PLAYWRIGHT_BROWSERS_PATH || '';
    let ff = 'ffmpeg';
    try { ff = findFilesUnder(root, 'ffmpeg') || 'ffmpeg'; } catch {}
    try {
      execFileSync(assertNotOption(ff, 'ffmpeg path'), [
        '-y', '-i', webm,
        '-vf', 'scale=1600:-2',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
        mp4,
      ], { stdio: 'ignore' });
    } catch (e) { mp4 = webm; }
  }
  console.log('SHOTS_JSON', JSON.stringify(shots));
  console.log('VIDEO', mp4 || webm || '<none>');
})().catch(e => { console.error('CAPTURE_ERR', e.message); process.exit(1); });
