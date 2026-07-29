import { chromium } from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const DEFAULT_VIEWPORTS = [
  [320, 568], [360, 640], [360, 800], [375, 667], [390, 844],
  [412, 915], [430, 932], [568, 320], [667, 375],
  [760, 900], [761, 900], [1024, 900], [1025, 900], [1440, 900],
];
const DEFAULT_FONT_SCALES = [100, 125, 150];
const PAGE_NAMES = ['monitor', 'execution', 'strategy', 'review', 'paper', 'search', 'aitrader', 'risk', 'settings'];

function readArg(name, fallback = null) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}
function hasArg(name) { return process.argv.includes(name); }
function parsePairs(value, fallback) {
  if (!value) return fallback;
  return value.split(',').map(item => item.split('x').map(Number)).filter(pair => pair.length === 2 && pair.every(Number.isFinite));
}
function parseNumbers(value, fallback) {
  if (!value) return fallback;
  return value.split(',').map(Number).filter(Number.isFinite);
}
function safeName(value) { return value.replace(/[^a-zA-Z0-9_.-]+/g, '-'); }
function isExpectedAuthConsole(message) {
  return /^Failed to load resource: the server responded with a status of (401|403|429) \(/.test(message);
}

const baseUrl = readArg('--base-url', 'http://127.0.0.1:5001');
const reportPath = path.resolve(readArg('--report', 'output/playwright/mobile-layout-audit.json'));
const screenshotDir = path.resolve(readArg('--screenshots', 'output/playwright/mobile-layout-audit'));
const quick = hasArg('--quick');
const direct = hasArg('--direct');
const hostIp = readArg('--host-ip');
const authConfigPath = readArg('--auth-config');
const viewports = parsePairs(readArg('--viewports'), quick ? [[320, 568], [761, 900], [1024, 900], [1025, 900], [1440, 900]] : DEFAULT_VIEWPORTS);
const fontScales = parseNumbers(readArg('--font-scales'), quick ? [100, 150] : DEFAULT_FONT_SCALES);
const pageNames = readArg('--pages') ? readArg('--pages').split(',').filter(Boolean) : PAGE_NAMES;
const chromePath = readArg('--chrome', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe');
const browserArgs = [];
if (direct) browserArgs.push('--no-proxy-server');
if (hostIp) browserArgs.push(`--host-resolver-rules=MAP ${new URL(baseUrl).hostname} ${hostIp}`);

await fs.mkdir(path.dirname(reportPath), { recursive: true });
await fs.mkdir(screenshotDir, { recursive: true });

const browser = await chromium.launch({ executablePath: chromePath, headless: true, args: browserArgs });
const results = [];
let authStorageState;

try {
  if (authConfigPath) {
    const credentials = JSON.parse(await fs.readFile(path.resolve(authConfigPath), 'utf8'));
    if (!credentials.username || !credentials.password) {
      throw new Error('Auth config must contain username and password');
    }
    const loginContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const loginPage = await loginContext.newPage();
    await loginPage.route('**/api/**', route => route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: '{"ok":false}',
    }));
    await loginPage.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await loginPage.unroute('**/api/**');
    const loginResult = await loginPage.evaluate(async payload => {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      return { status: response.status, ok: body.ok === true };
    }, { username: credentials.username, password: credentials.password });
    if (loginResult.status !== 200 || !loginResult.ok) {
      throw new Error(`Production audit login failed with HTTP ${loginResult.status}`);
    }
    authStorageState = await loginContext.storageState();
    await loginContext.close();
  }

  for (const [width, height] of viewports) {
    for (const fontScale of fontScales) {
      const context = await browser.newContext({
        viewport: { width, height },
        deviceScaleFactor: 1,
        storageState: authStorageState,
      });
      const page = await context.newPage();
      const cdp = await context.newCDPSession(page);
      await cdp.send('Page.setBypassCSP', { enabled: true });
      const runtime = { pageErrors: [], consoleErrors: [], failedRequests: [], badResponses: [] };
      page.on('pageerror', error => runtime.pageErrors.push(String(error.message || error)));
      page.on('console', message => {
        if (message.type() !== 'error') return;
        const text = message.text();
        if (!isExpectedAuthConsole(text)) runtime.consoleErrors.push(text);
      });
      page.on('requestfailed', request => {
        const type = request.resourceType();
        if (['document', 'stylesheet', 'script', 'font', 'image'].includes(type)) {
          runtime.failedRequests.push(`${type} ${request.url()} ${request.failure()?.errorText || ''}`);
        }
      });
      page.on('response', response => {
        const type = response.request().resourceType();
        if (response.status() >= 500 || (response.status() >= 400 && ['document', 'stylesheet', 'script', 'font'].includes(type))) {
          runtime.badResponses.push(`${response.status()} ${type} ${response.url()}`);
        }
      });

      await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.addStyleTag({ content: `
        .auth-overlay { display:none !important; }
        html { font-size:100% !important; }
        body.audit-font-scale button, body.audit-font-scale input,
        body.audit-font-scale select, body.audit-font-scale textarea { font-size:${fontScale / 100}em !important; }
      ` });
      await page.evaluate(scale => {
        document.body.classList.add('audit-font-scale');
        document.documentElement.dataset.auditFontScale = String(scale);
      }, fontScale);
      await page.waitForTimeout(250);

      for (const pageName of pageNames) {
        const beforeErrors = {
          pageErrors: runtime.pageErrors.length,
          consoleErrors: runtime.consoleErrors.length,
          failedRequests: runtime.failedRequests.length,
          badResponses: runtime.badResponses.length,
        };
        const switched = await page.evaluate(name => {
          const tab = document.querySelector(`.side .ntab[data-page="${name}"]`);
          if (!tab) return false;
          tab.click();
          return true;
        }, pageName);
        await page.waitForTimeout(180);

        const audit = await page.evaluate(({ width, height, pageName }) => {
          const visible = element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
          };
          const label = element => {
            const id = element.id ? `#${element.id}` : '';
            const classes = [...element.classList].slice(0, 3).map(item => `.${item}`).join('');
            return `${element.tagName.toLowerCase()}${id}${classes}`;
          };
          const effectiveTouchRect = element => {
            if (element.matches('input[type="checkbox"],input[type="radio"]')) {
              const wrapper = element.closest('label');
              if (wrapper && visible(wrapper)) return wrapper.getBoundingClientRect();
            }
            return element.getBoundingClientRect();
          };
          const active = document.querySelector(`#page-${pageName}.active`);
          const problems = [];
          const doc = document.documentElement;
          if (doc.scrollWidth > doc.clientWidth + 1) {
            problems.push({ type: 'document-overflow', actual: doc.scrollWidth, limit: doc.clientWidth });
          }
          if (!active) problems.push({ type: 'page-not-active', pageName });

          if (active) {
            const nodes = [active, ...active.querySelectorAll('*')];
            for (const element of nodes) {
              if (!visible(element)) continue;
              const rect = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              const isTablePart = ['TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD'].includes(element.tagName);
              const isCodeMirrorPart = Boolean(element.closest('.CodeMirror'));
              const scrollParent = element.closest('.tbl-wrap,.table-wrap,.card-bd,.exec-body-panel,[style*="overflow"]');
              const locallyScrollable = scrollParent && ['auto', 'scroll'].includes(getComputedStyle(scrollParent).overflowX);
              if (!isTablePart && !isCodeMirrorPart && !locallyScrollable && (rect.right > width + 1 || rect.left < -1)) {
                problems.push({ type: 'viewport-overflow', element: label(element), left: Math.round(rect.left), right: Math.round(rect.right), width });
              }
              if (element.scrollWidth > element.clientWidth + 4 && !['auto', 'scroll'].includes(style.overflowX) && !isTablePart && !isCodeMirrorPart) {
                problems.push({ type: 'unhandled-inner-overflow', element: label(element), scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, overflowX: style.overflowX });
              }
            }
          }

          if (document.body.classList.contains('mobile-layout')) {
            const controls = active ? [...active.querySelectorAll('button,input:not([type="hidden"]),select,textarea,[role="button"]')] : [];
            for (const element of controls) {
              if (!visible(element) || element.disabled) continue;
              if (element.closest('.CodeMirror')) continue;
              const rect = effectiveTouchRect(element);
              if (rect.width < 44 || rect.height < 44) {
                problems.push({ type: 'small-target', element: label(element), size: `${Math.round(rect.width)}x${Math.round(rect.height)}` });
              }
            }
            const header = document.getElementById('mobileHeader');
            const copy = header?.querySelector('.mobile-header-copy')?.getBoundingClientRect();
            const control = document.getElementById('mobileRunTrigger')?.getBoundingClientRect();
            if (!header || getComputedStyle(header).display === 'none') problems.push({ type: 'mobile-header-hidden' });
            if (copy && control && copy.right > control.left) problems.push({ type: 'header-overlap', copyRight: copy.right, controlLeft: control.left });
            const dock = document.getElementById('mobileDock');
            if (!dock || getComputedStyle(dock).display === 'none') problems.push({ type: 'mobile-dock-hidden' });
            const desktopNav = document.querySelector('.nav');
            const desktopSide = document.querySelector('.side');
            if (desktopNav && getComputedStyle(desktopNav).display !== 'none') problems.push({ type: 'desktop-nav-visible' });
            if (desktopSide && getComputedStyle(desktopSide).display !== 'none') problems.push({ type: 'desktop-side-visible' });
          } else {
            const header = document.getElementById('mobileHeader');
            const dock = document.getElementById('mobileDock');
            if (header && getComputedStyle(header).display !== 'none') problems.push({ type: 'mobile-header-on-desktop' });
            if (dock && getComputedStyle(dock).display !== 'none') problems.push({ type: 'mobile-dock-on-desktop' });
          }

          return {
            problems,
            dimensions: {
              viewport: [width, height],
              document: [doc.scrollWidth, doc.scrollHeight],
              bodyClass: document.body.className,
              activePage: active?.id || null,
            },
          };
        }, { width, height, pageName });

        if (width <= 1024 && pageName === 'monitor') {
          for (const sheetName of ['mobileRunSheet', 'mobileMoreSheet']) {
            await page.evaluate(id => {
              if (id === 'mobileRunSheet') window.toggleMobileRunSheet(true);
              else window.toggleMobileMore(true);
            }, sheetName);
            await page.waitForTimeout(320);
            const sheetAudit = await page.evaluate(({ id, width, height }) => {
              const sheet = document.getElementById(id);
              const panel = sheet?.querySelector('section');
              if (!sheet || !panel) return { type: 'sheet-missing', id };
              const rect = panel.getBoundingClientRect();
              if (rect.left < -1 || rect.right > width + 1 || rect.top < -1 || rect.bottom > height + 1) {
                return { type: 'sheet-overflow', id, rect: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom }, viewport: [width, height] };
              }
              return null;
            }, { id: sheetName, width, height });
            if (sheetAudit) audit.problems.push(sheetAudit);
            await page.evaluate(id => {
              if (id === 'mobileRunSheet') window.toggleMobileRunSheet(false);
              else window.toggleMobileMore(false);
            }, sheetName);
          }
        }

        const newRuntime = {
          pageErrors: runtime.pageErrors.slice(beforeErrors.pageErrors),
          consoleErrors: runtime.consoleErrors.slice(beforeErrors.consoleErrors),
          failedRequests: runtime.failedRequests.slice(beforeErrors.failedRequests),
          badResponses: runtime.badResponses.slice(beforeErrors.badResponses),
        };
        const entry = { viewport: [width, height], fontScale, pageName, switched, ...audit, runtime: newRuntime };
        entry.ok = switched && audit.problems.length === 0 && Object.values(newRuntime).every(items => items.length === 0);
        results.push(entry);

        if (!entry.ok && hasArg('--screenshots')) {
          const filename = safeName(`${width}x${height}-${fontScale}-${pageName}.png`);
          await page.screenshot({ path: path.join(screenshotDir, filename), fullPage: false });
          entry.screenshot = path.join(screenshotDir, filename);
        }
      }
      await context.close();
    }
  }
} finally {
  await browser.close();
}

const failures = results.filter(item => !item.ok);
const summary = {
  generatedAt: new Date().toISOString(),
  baseUrl,
  total: results.length,
  passed: results.length - failures.length,
  failed: failures.length,
  failuresByType: {},
};
for (const item of failures) {
  for (const problem of item.problems) summary.failuresByType[problem.type] = (summary.failuresByType[problem.type] || 0) + 1;
  for (const [kind, values] of Object.entries(item.runtime)) if (values.length) summary.failuresByType[kind] = (summary.failuresByType[kind] || 0) + values.length;
}
await fs.writeFile(reportPath, JSON.stringify({ summary, results }, null, 2));
console.log(JSON.stringify(summary, null, 2));
if (failures.length) process.exitCode = 1;
