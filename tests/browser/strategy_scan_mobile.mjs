import path from 'node:path';
import process from 'node:process';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs/promises';

function readArg(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const baseUrl = readArg('--base-url', 'http://127.0.0.1:5001');
const chromePath = readArg('--chrome', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe');
const viewportWidth = Number(readArg('--width', '390'));
const screenshotPath = readArg('--screenshot', '');
const bundledModules = process.env.CODEX_NODE_MODULES;
const playwright = bundledModules
  ? await import(pathToFileURL(path.join(bundledModules, 'playwright', 'index.mjs')))
  : await import('playwright');
const { chromium } = playwright;
const browser = await chromium.launch({ executablePath: chromePath, headless: true });
let scanStarts = 0;
let progressReads = 0;

try {
  const context = await browser.newContext({
    viewport: { width: viewportWidth, height: 844 },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, '');
    let body = { ok: true };

    if (path === '/auth/me') body = { logged_in: false };
    else if (path === '/strategies') body = [];
    else if (path === '/data/stocks') body = [];
    else if (path.startsWith('/market/context')) body = { ok: true, strength: 50, trend: '震荡' };
    else if (path === '/strategies/run_async') {
      scanStarts += 1;
      body = { ok: true, task_id: 'mobile-scan-task' };
    } else if (path === '/strategies/progress/mobile-scan-task') {
      progressReads += 1;
      body = progressReads >= 2
        ? { ok: true, status: 'done', scan_current: 2, scan_total: 2, scan_stage: 'strategy' }
        : { ok: true, status: 'running', scan_current: 1, scan_total: 2, scan_stage: 'strategy' };
    } else if (path === '/strategies/result/mobile-scan-task') {
      body = {
        ok: true,
        results: [{
          code: '000001',
          name: '\u5e73\u5b89\u94f6\u884c',
          price: 10.25,
          change: 1.8,
          score: 82,
          reason: '\u5747\u7ebf\u591a\u5934',
        }],
        count: 1,
        scanned: 2,
      };
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });

  await page.goto(baseUrl, { waitUntil: 'load', timeout: 30000 });
  await page.evaluate(() => {
    document.getElementById('authOverlay').style.display = 'none';
    switchPage('strategy', document.querySelector('.ntab[data-page="strategy"]'));
    setMobileStrategyPane('editor');
    curStrat = 'da_ban';
    _curMode = 'strat';
  });

  await page.evaluate(() => {
    window.runScreen(600);
    window.runScreen(600);
  });

  await page.waitForFunction(
    () => document.querySelector('.strat-wrap')?.dataset.mobilePane === 'result',
    null,
    { timeout: 3000 },
  );
  await page.waitForFunction(
    () => document.getElementById('scInfo')?.textContent.includes('扫描 2 只'),
    null,
    { timeout: 7000 },
  );

  const state = await page.evaluate(() => ({
    pane: document.querySelector('.strat-wrap')?.dataset.mobilePane,
    info: document.getElementById('scInfo')?.textContent,
    progressVisible: getComputedStyle(document.getElementById('scProgWrap')).display !== 'none',
    scanButtonsDisabled: [...document.querySelectorAll('[data-ui-click^="runScreen("]')]
      .every(button => button.disabled === false),
    stockName: document.querySelector('.sc-stock-name')?.textContent?.trim(),
    stockNameWidth: document.querySelector('.sc-stock-name')?.getBoundingClientRect().width || 0,
  }));

  if (scanStarts !== 1) {
    throw new Error(`重复点击启动了 ${scanStarts} 个扫描任务，预期 1 个`);
  }
  if (state.pane !== 'result') {
    throw new Error(`手机端未切到结果工作区，当前为 ${state.pane}`);
  }
  if (!state.info.includes('扫描 2 只，命中 1 只')) {
    throw new Error(`扫描结果状态异常: ${state.info}`);
  }
  if (state.progressVisible) {
    throw new Error('扫描完成后进度条仍然可见');
  }
  if (!state.scanButtonsDisabled) {
    throw new Error('扫描完成后按钮没有恢复可用');
  }
  if (state.stockName !== '\u5e73\u5b89\u94f6\u884c' || state.stockNameWidth < 56) {
    throw new Error(`\u624b\u673a\u7aef\u80a1\u7968\u540d\u79f0\u4e0d\u53ef\u89c1: ${JSON.stringify({
      name: state.stockName,
      width: state.stockNameWidth,
    })}`);
  }
  if (screenshotPath) {
    const resolvedScreenshot = path.resolve(screenshotPath);
    await fs.mkdir(path.dirname(resolvedScreenshot), { recursive: true });
    await page.screenshot({ path: resolvedScreenshot, fullPage: false });
  }

  console.log(JSON.stringify({
    ok: true,
    scanStarts,
    progressReads,
    viewportWidth,
    pane: state.pane,
    info: state.info,
    stockName: state.stockName,
    stockNameWidth: state.stockNameWidth,
  }));
} finally {
  await browser.close();
}
