/**
 * AIFactory screenshot capture for the docs site.
 *
 * Drives a running portal via Playwright (headless Chromium) and saves a
 * curated set of PNGs to docs/static/img/screenshots/.
 *
 * Auth + navigation (proven recipe against the live portal):
 *   1. The portal gates on a login screen. The reliable path is to type the
 *      API token into the `#token` field and click "Login". localStorage
 *      pre-seeding alone does NOT work — the route guard redirects to /login
 *      before the auth store hydrates, and a hard navigation to a deep route
 *      bounces back to /login. So we log in once via the form.
 *   2. After login the app keeps auth only for *in-SPA* navigation. We never
 *      `page.goto` a deep route after login — all view changes go through the
 *      in-app nav buttons and the project switcher (a `[role=combobox]`).
 *   3. To change project we open the combobox and pick the project by name,
 *      not by URL.
 *
 * Run with:
 *   AIFACTORY_PORTAL_URL=https://aifactory.freundcloud.org.uk \
 *   AIFACTORY_PROJECT=olafkfreund-aifactory-demo \
 *   AIFACTORY_FAILED_PROJECT=factory-demo-taskboard \
 *   PLAYWRIGHT_CHROMIUM_EXECUTABLE=<chrome path> \
 *   npm -w apps/frontend-web run capture-screenshots
 *
 * Token is read from ~/.aifactory/.token (mint from the cluster secret
 * factory-secrets/APP_API_TOKEN). Chromium falls back to the Nix
 * google-chrome-stable when the Playwright-bundled binary is unavailable.
 *
 * The script is intentionally tolerant — if a view or selector misses it
 * logs a warning and continues, capturing what it can.
 */

import {chromium, type Browser, type Page} from '@playwright/test';
import * as path from 'node:path';
import * as fs from 'node:fs';

const PORTAL_URL = process.env.AIFACTORY_PORTAL_URL ?? 'http://localhost:3100';

// A project whose lane holds completed / human-review (success-path) tasks
// plus a running one.
const PROJECT = process.env.AIFACTORY_PROJECT ?? 'olafkfreund-aifactory-demo';

// A project that has a task in an error / failed state, for the "failed run"
// shots.
const FAILED_PROJECT =
  process.env.AIFACTORY_FAILED_PROJECT ?? 'factory-demo-taskboard';

const TOKEN_FILE = path.join(process.env.HOME ?? '/root', '.aifactory', '.token');

const OUT_DIR = path.resolve(
  __dirname,
  '..',
  'docs',
  'static',
  'img',
  'screenshots'
);

const CARD_SELECTOR = '.rounded-xl.border.bg-card';

// ---------- helpers ----------

function loadToken(): string {
  if (!fs.existsSync(TOKEN_FILE)) {
    throw new Error(
      `Token not found at ${TOKEN_FILE}. ` +
        `Mint it: kubectl get secret factory-secrets -n factory ` +
        `-o jsonpath='{.data.APP_API_TOKEN}' | base64 -d > ${TOKEN_FILE}`
    );
  }
  return fs.readFileSync(TOKEN_FILE, 'utf-8').trim();
}

const TOKEN = loadToken();

async function shoot(page: Page, name: string): Promise<void> {
  await page.screenshot({path: path.join(OUT_DIR, name), fullPage: false});
  // eslint-disable-next-line no-console
  console.log(`  + ${name}`);
}

async function withFallback(name: string, fn: () => Promise<void>): Promise<void> {
  try {
    await fn();
  } catch (e) {
    console.warn(`  ! ${name} failed: ${(e as Error).message}`);
  }
}

function onLoginScreen(page: Page): Promise<boolean> {
  return page
    .locator('#token')
    .isVisible({timeout: 1500})
    .catch(() => false);
}

async function waitForApp(page: Page): Promise<void> {
  await page.waitForLoadState('networkidle').catch(() => {});
  await page
    .waitForFunction(
      () => !/Preparing your workspace/i.test(document.body.innerText),
      {timeout: 10_000}
    )
    .catch(() => {});
  await page.waitForTimeout(700);
}

async function login(page: Page): Promise<void> {
  await page.goto(`${PORTAL_URL}/login`);
  await page.waitForTimeout(1200);
  if (await onLoginScreen(page)) {
    await page.locator('#token').fill(TOKEN);
    await page
      .getByRole('button', {name: /^login$/i})
      .first()
      .click();
    await page.waitForTimeout(2500);
  }
  await waitForApp(page);
}

async function currentProject(page: Page): Promise<string> {
  return (
    (await page.getByRole('combobox').first().textContent().catch(() => '')) ?? ''
  ).trim();
}

/** Switch the active project via the in-app combobox (no navigation). */
async function selectProject(page: Page, name: string): Promise<void> {
  if ((await currentProject(page)).includes(name)) {
    return;
  }
  const combo = page.getByRole('combobox').first();
  await combo.click();
  await page.waitForTimeout(800);
  const opt = page.getByRole('option', {name: new RegExp(name, 'i')}).first();
  if (await opt.isVisible({timeout: 2500}).catch(() => false)) {
    await opt.click();
    await page.waitForTimeout(2500);
    await waitForApp(page);
  } else {
    await page.keyboard.press('Escape').catch(() => {});
  }
}

async function clickNav(page: Page, re: RegExp): Promise<boolean> {
  const btn = page.getByRole('button', {name: re}).first();
  if (await btn.isVisible({timeout: 3000}).catch(() => false)) {
    await btn.click();
    await page.waitForTimeout(1300);
    return true;
  }
  return false;
}

async function openFirstTask(page: Page): Promise<boolean> {
  const card = page.locator(CARD_SELECTOR).first();
  if (!(await card.isVisible({timeout: 4000}).catch(() => false))) {
    return false;
  }
  const heading = card.locator('h3').first();
  const target = (await heading.isVisible({timeout: 1500}).catch(() => false))
    ? heading
    : card;
  await target.click();
  await page.waitForTimeout(1500);
  return page
    .locator('[role="dialog"]')
    .first()
    .isVisible({timeout: 3000})
    .catch(() => false);
}

async function clickTab(page: Page, re: RegExp): Promise<boolean> {
  const tab = page.getByRole('tab', {name: re}).first();
  if (await tab.isVisible({timeout: 3000}).catch(() => false)) {
    await tab.click();
    await page.waitForTimeout(900);
    return true;
  }
  return false;
}

/** Click a left-rail entry inside the Settings dialog by its label. */
async function settingsSection(page: Page, re: RegExp): Promise<boolean> {
  const item = page.getByText(re).first();
  if (await item.isVisible({timeout: 2500}).catch(() => false)) {
    await item.click();
    await page.waitForTimeout(700);
    return true;
  }
  return false;
}

async function closeDialog(page: Page): Promise<void> {
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(500);
}

// ---------- main ----------

async function main(): Promise<void> {
  fs.mkdirSync(OUT_DIR, {recursive: true});

  const browser: Browser = await chromium.launch({
    headless: true,
    executablePath:
      process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ??
      '/etc/profiles/per-user/olafkfreund/bin/google-chrome-stable',
  });
  const context = await browser.newContext({
    viewport: {width: 1440, height: 900},
  });
  const page = await context.newPage();

  console.log(`Capturing screenshots to ${OUT_DIR}`);

  // --- 01 login (captured before authenticating) ---
  await withFallback('01-login.png', async () => {
    await page.goto(`${PORTAL_URL}/login`);
    await waitForApp(page);
    if (await onLoginScreen(page)) {
      await shoot(page, '01-login.png');
    }
  });

  // --- authenticate once; everything after is in-SPA ---
  await login(page);
  await selectProject(page, PROJECT);

  await withFallback('02-kanban-board.png', async () => {
    await clickNav(page, /^\s*tasks\s*$/i);
    await shoot(page, '02-kanban-board.png');
  });

  await withFallback('03-task-create.png', async () => {
    const taskBtn = page.getByRole('button', {name: /^\s*task\s*$/i}).last();
    if (await taskBtn.isVisible({timeout: 4000}).catch(() => false)) {
      await taskBtn.click();
      await page.waitForTimeout(1100);
      await shoot(page, '03-task-create.png');
      await closeDialog(page);
    }
  });

  // Task detail: open once, walk the tabs while it stays open.
  await withFallback('04-task-detail-overview.png', async () => {
    if (await openFirstTask(page)) {
      await shoot(page, '04-task-detail-overview.png');
    }
  });
  await withFallback('05-task-detail-subtasks.png', async () => {
    if (await clickTab(page, /subtasks/i)) {
      await shoot(page, '05-task-detail-subtasks.png');
    }
  });
  await withFallback('06-task-detail-logs.png', async () => {
    if (await clickTab(page, /^logs$/i)) {
      await shoot(page, '06-task-detail-logs.png');
    }
  });
  await withFallback('07-task-detail-files.png', async () => {
    if (await clickTab(page, /^files$/i)) {
      await page.waitForTimeout(700);
      await shoot(page, '07-task-detail-files.png');
    }
  });
  await withFallback('08-task-detail-observability.png', async () => {
    if (await clickTab(page, /observability/i)) {
      await page.waitForTimeout(1200);
      await shoot(page, '08-task-detail-observability.png');
    }
    await closeDialog(page);
  });

  // Failed / error project.
  await withFallback('09-failed-task-board.png', async () => {
    await selectProject(page, FAILED_PROJECT);
    await clickNav(page, /^\s*tasks\s*$/i);
    await shoot(page, '09-failed-task-board.png');
  });
  await withFallback('10-failed-task-detail.png', async () => {
    if (await openFirstTask(page)) {
      await page.waitForTimeout(700);
      await shoot(page, '10-failed-task-detail.png');
      if (await clickTab(page, /^logs$/i)) {
        await shoot(page, '11-failed-task-logs.png');
      }
      await closeDialog(page);
    }
  });

  // Back to the demo project for the remaining feature views.
  await selectProject(page, PROJECT);

  const navShots: Array<[string, RegExp]> = [
    ['12-files.png', /^\s*files\s*$/i],
    ['13-chat.png', /^\s*chat\s*$/i],
    ['14-terminal.png', /^\s*terminal\s*$/i],
    ['15-mcp.png', /^\s*mcp\s*$/i],
    ['16-skills.png', /^\s*skills\s*$/i],
    ['17-worktrees.png', /worktrees/i],
    ['18-index-memory.png', /index\s*&?\s*memory/i],
    ['19-github-issues.png', /github\s*issues/i],
    ['20-github-prs.png', /github\s*prs/i],
  ];
  for (const [name, re] of navShots) {
    await withFallback(name, async () => {
      if (await clickNav(page, re)) {
        await page.waitForTimeout(700);
        await shoot(page, name);
      }
    });
  }

  // Settings dialog sections.
  await withFallback('21-settings-agent.png', async () => {
    await clickNav(page, /^\s*tasks\s*$/i);
    const gear = page.locator('button[aria-label="Project settings"]').first();
    if (await gear.isVisible({timeout: 4000}).catch(() => false)) {
      await gear.click();
      await page.waitForTimeout(1100);
      await settingsSection(page, /^Agent Settings$/i);
      await shoot(page, '21-settings-agent.png');
    }
  });
  await withFallback('22-settings-llm-providers.png', async () => {
    if (await settingsSection(page, /^LLM Providers$/i)) {
      await shoot(page, '22-settings-llm-providers.png');
    }
  });
  await withFallback('23-settings-integrations.png', async () => {
    if (await settingsSection(page, /^Integrations$/i)) {
      await shoot(page, '23-settings-integrations.png');
    }
    await closeDialog(page);
  });

  await browser.close();
  console.log('\nDone. Screenshots saved to:', OUT_DIR);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
