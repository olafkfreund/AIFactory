/**
 * AIFactory screenshot capture for the docs site.
 *
 * Drives the running portal at http://localhost:3100 via Playwright
 * (headless Chromium) and saves ~14 PNGs to docs/static/img/screenshots/.
 *
 * Run with:
 *   1. Start the portal stack: web-server on :3101, vite dev on :3100
 *   2. Run the demo: ./scripts/demo.sh --yolo  (seeds 3 tasks)
 *   3. npm -w apps/frontend-web run capture-screenshots
 *
 * The script is intentionally tolerant — if a view 404s or a selector
 * misses, it logs a warning and continues, capturing what it can.
 */

import {chromium, type Browser, type Page} from '@playwright/test';
import * as path from 'node:path';
import * as fs from 'node:fs';

const PORTAL_URL = process.env.AIFACTORY_PORTAL_URL ?? 'http://localhost:3100';
// Jump straight to a project's board instead of the picker.
const PROJECT_ID =
  process.env.AIFACTORY_PROJECT_ID ?? 'f7ac8d99-b913-4c6f-afce-f8376e29c98c';
const TOKEN_FILE = path.join(
  process.env.HOME ?? '/root',
  '.aifactory',
  '.token'
);
const OUT_DIR = path.resolve(
  __dirname,
  '..',
  'docs',
  'static',
  'img',
  'screenshots'
);

interface Shot {
  name: string;
  description: string;
  capture: (page: Page) => Promise<void>;
}

// ---------- helpers ----------

function loadToken(): string {
  if (!fs.existsSync(TOKEN_FILE)) {
    throw new Error(
      `Token not found at ${TOKEN_FILE}. Start the portal once to create it.`
    );
  }
  return fs.readFileSync(TOKEN_FILE, 'utf-8').trim();
}

async function shoot(page: Page, name: string): Promise<void> {
  const outPath = path.join(OUT_DIR, name);
  await page.screenshot({path: outPath, fullPage: false});
  // eslint-disable-next-line no-console
  console.log(`  ✓ ${name}`);
}

async function withFallback(
  name: string,
  fn: () => Promise<void>
): Promise<void> {
  try {
    await fn();
  } catch (e) {
    console.warn(`  ⚠ ${name} failed: ${(e as Error).message}`);
  }
}

/**
 * After a full page load the app shows a ~2s animated LoadingScreen
 * ("Preparing your workspace…") before the board renders. Wait for it to
 * clear so subsequent selectors resolve against the real UI.
 */
async function waitForApp(page: Page): Promise<void> {
  await page.waitForLoadState('networkidle').catch(() => {});
  await page
    .waitForFunction(
      () => !/Preparing your workspace/i.test(document.body.innerText),
      {timeout: 10_000}
    )
    .catch(() => {});
  await page.waitForTimeout(800);
}

// ---------- shot definitions ----------

const SHOTS: Shot[] = [
  {
    name: '01-welcome.png',
    description: 'Welcome screen with the project list',
    capture: async (page) => {
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      await shoot(page, '01-welcome.png');
    },
  },
  {
    name: '02-onboarding-providers.png',
    description: 'Onboarding wizard — provider choice step',
    capture: async (page) => {
      // Try opening Settings → Agent Profile as a proxy for the
      // provider-choice surface (the onboarding wizard runs once).
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      const settings = page.getByRole('button', {name: /settings/i}).first();
      if (await settings.isVisible({timeout: 3000})) {
        await settings.click();
        await page.waitForTimeout(500);
        await shoot(page, '02-onboarding-providers.png');
      }
    },
  },
  {
    name: '03-kanban.png',
    description: 'Kanban board with seeded demo tasks',
    capture: async (page) => {
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      // Click first project card if Welcome screen is showing
      const projectCard = page
        .locator('[data-testid^="project-card-"]')
        .first();
      if (await projectCard.isVisible({timeout: 3000})) {
        await projectCard.click();
        await waitForApp(page);
      }
      await shoot(page, '03-kanban.png');
    },
  },
  {
    name: '04-task-create-wizard.png',
    description: 'Task creation wizard, step 1',
    capture: async (page) => {
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      // The "+ Task" button lives at the bottom of the sidebar (label "Task").
      const newTaskBtn = page
        .getByRole('button', {name: /^\s*(new task|create task|task)\s*$/i})
        .last();
      if (await newTaskBtn.isVisible({timeout: 5000})) {
        await newTaskBtn.click();
        await page.waitForTimeout(900);
        await shoot(page, '04-task-create-wizard.png');
        await page.keyboard.press('Escape');
        await page.waitForTimeout(400);
      }
    },
  },
  {
    name: '05-task-detail-overview.png',
    description: 'Task detail modal — overview tab (completed task)',
    capture: async (page) => {
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      // Prefer a completed task (rich plan/logs/files); fall back to any card.
      let card = page
        .locator('.task-card-enhanced')
        .filter({hasText: /Tic-Tac-Toe/i})
        .first();
      if (!(await card.isVisible({timeout: 3000}).catch(() => false))) {
        card = page.locator('.task-card-enhanced').first();
      }
      if (await card.isVisible({timeout: 3000})) {
        await card.click();
        await page.waitForTimeout(1200);
        await shoot(page, '05-task-detail-overview.png');
      }
    },
  },
  {
    name: '06-task-detail-plan.png',
    description: 'Plan / subtasks tab',
    capture: async (page) => {
      // Tabs (in order): overview, subtasks, logs, files, agent-console.
      const tab = page.getByRole('tab').nth(1); // subtasks = the plan
      if (await tab.isVisible({timeout: 3000})) {
        await tab.click();
        await page.waitForTimeout(800);
        await shoot(page, '06-task-detail-plan.png');
      }
    },
  },
  {
    name: '07-task-detail-logs.png',
    description: 'Phase logs',
    capture: async (page) => {
      const tab = page.getByRole('tab').nth(2); // logs
      if (await tab.isVisible({timeout: 3000})) {
        await tab.click();
        await page.waitForTimeout(800);
        await shoot(page, '07-task-detail-logs.png');
      }
    },
  },
  {
    name: '08-task-detail-files.png',
    description: 'Files tab',
    capture: async (page) => {
      const tab = page.getByRole('tab', {name: /files/i}).first();
      if (await tab.isVisible({timeout: 3000})) {
        await tab.click();
        await page.waitForTimeout(1200);
        await shoot(page, '08-task-detail-files.png');
      }
      // Close the detail modal before the live-console shot reopens another.
      await page.keyboard.press('Escape');
      await page.waitForTimeout(400);
    },
  },
  {
    name: '09-live-agent-console.png',
    description: 'Live Agent Console (running task)',
    capture: async (page) => {
      // Open a task that is actively building so the Live Console tab renders.
      let card = page
        .locator('.task-card-enhanced')
        .filter({hasText: /version|healthz|status|endpoint/i})
        .first();
      if (!(await card.isVisible({timeout: 3000}).catch(() => false))) {
        card = page.locator('.task-card-enhanced').first();
      }
      await card.click();
      await page.waitForTimeout(1000);
      const consoleTab = page
        .getByRole('tab', {name: /live console|agent console/i})
        .first();
      if (await consoleTab.isVisible({timeout: 3000})) {
        await consoleTab.click();
        await page.waitForTimeout(2500); // give the WS/PTY time to connect
        await shoot(page, '09-live-agent-console.png');
      } else {
        console.warn(
          '  ⚠ Live Console tab not visible — task may not be running.'
        );
      }
      await page.keyboard.press('Escape');
      await page.waitForTimeout(400);
    },
  },
  {
    name: '10-terminal-grid.png',
    description: 'Multi-pane terminal grid',
    capture: async (page) => {
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      const terminalsNav = page.getByRole('button', {name: /terminal/i}).first();
      if (await terminalsNav.isVisible({timeout: 3000})) {
        await terminalsNav.click();
        await page.waitForTimeout(1500);
        await shoot(page, '10-terminal-grid.png');
      }
    },
  },
  {
    name: '11-github-issues-sync.png',
    description: 'GitHub Issues view',
    capture: async (page) => {
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      const githubNav = page
        .getByRole('button', {name: /github\s*issues/i})
        .first();
      if (await githubNav.isVisible({timeout: 3000})) {
        await githubNav.click();
        await page.waitForTimeout(1500);
        await shoot(page, '11-github-issues-sync.png');
      }
    },
  },
  {
    name: '12-settings-llm-providers.png',
    description: 'LLM Providers settings',
    capture: async (page) => {
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      const settings = page.getByRole('button', {name: /settings/i}).first();
      if (await settings.isVisible({timeout: 3000})) {
        await settings.click();
        await page.waitForTimeout(700);
        const llm = page
          .getByRole('button', {name: /llm providers/i})
          .first();
        if (await llm.isVisible({timeout: 2000})) {
          await llm.click();
          await page.waitForTimeout(700);
        }
        await shoot(page, '12-settings-llm-providers.png');
      }
    },
  },
  {
    name: '13-settings-mcp-servers.png',
    description: 'MCP servers settings',
    capture: async (page) => {
      // Settings dialog should still be open from shot 12.
      const mcp = page.getByRole('button', {name: /^\s*mcp/i}).first();
      if (await mcp.isVisible({timeout: 2000})) {
        await mcp.click();
        await page.waitForTimeout(700);
        await shoot(page, '13-settings-mcp-servers.png');
      }
      await page.keyboard.press('Escape');
      await page.waitForTimeout(400);
    },
  },
  {
    name: '14-insights-chat.png',
    description: 'Insights / chat',
    capture: async (page) => {
      await page.goto(PORTAL_URL);
      await waitForApp(page);
      const chatNav = page
        .getByRole('button', {name: /^\s*(chat|insights)\s*$/i})
        .first();
      if (await chatNav.isVisible({timeout: 3000})) {
        await chatNav.click();
        await page.waitForTimeout(1200);
        await shoot(page, '14-insights-chat.png');
      }
    },
  },
];

// ---------- main ----------

async function main(): Promise<void> {
  fs.mkdirSync(OUT_DIR, {recursive: true});

  const token = loadToken();

  // On NixOS the Playwright-bundled chromium/headless-shell isn't provided;
  // use the system Chrome (matches record-portal-walkthrough.ts).
  const browser: Browser = await chromium.launch({
    headless: true,
    executablePath:
      process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ??
      '/etc/profiles/per-user/olafkfreund/bin/google-chrome-stable',
  });
  const context = await browser.newContext({
    viewport: {width: 1440, height: 900},
    // Inject token so the portal is pre-authenticated
    extraHTTPHeaders: {Authorization: `Bearer ${token}`},
    storageState: {
      cookies: [],
      origins: [
        {
          origin: PORTAL_URL,
          localStorage: [
            // Canonical token key the app reads (see src/lib/auth.ts → 'aifactory-token').
            {name: 'aifactory-token', value: token},
            {name: 'aifactory.token', value: token},
            {name: 'aifactory-theme', value: 'dark'},
            // Land directly on a project's board (see project-store.ts).
            {name: 'lastSelectedProjectId', value: PROJECT_ID},
            // Skip the onboarding wizard on subsequent shots
            {name: 'aifactory.onboarding.completed', value: 'true'},
          ],
        },
      ],
    },
  });

  const page = await context.newPage();

  console.log(`Capturing ${SHOTS.length} screenshots to ${OUT_DIR}`);

  for (const shot of SHOTS) {
    await withFallback(shot.name, () => shot.capture(page));
  }

  await browser.close();
  console.log('\nDone. Screenshots saved to:', OUT_DIR);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
