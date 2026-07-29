import { chromium } from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const baseUrl = process.argv[2] || 'http://127.0.0.1:5001';
const chromePath = process.argv[3] || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const outDir = path.resolve('output/playwright/research-mobile-layout');
await fs.mkdir(outDir, { recursive: true });

const concepts = Array.from({ length: 36 }, (_, index) => ({
  name: `Concept ${index + 1} with a deliberately long mobile label`,
  chg: Number(((index % 7) - 3) * 1.27).toFixed(2),
  up: 8 + index % 11,
  down: 2 + index % 5,
}));
const stocks = Array.from({ length: 28 }, (_, index) => ({
  code: String(600000 + index),
  name: `Listed Company ${index + 1} Long Name`,
  rel_level: ['\u6838\u5fc3', '\u91cd\u8981', '\u4e00\u822c'][index % 3],
  rel_reason: 'Long relation reason used to verify wrapping on a narrow viewport.',
  price: 10.21 + index,
  chg: Number(((index % 9) - 4) * 0.83).toFixed(2),
  turnover: Number(2.4 + index * 0.31).toFixed(2),
  mcap: Math.round(80 + index * 31.7),
}));
const reports = Array.from({ length: 24 }, (_, index) => ({
  title: `${index + 1}. Deep industry-chain research report with a long title that must wrap cleanly on mobile`,
  stock: index % 2 ? 'Seagull Tech' : '',
  code: index % 2 ? '600001' : '',
  industry: index % 2 ? '' : 'Advanced Manufacturing and Artificial Intelligence',
  date: `2026-07-${String(28 - index % 20).padStart(2, '0')}`,
  org: 'Research Institute with a Long Name',
  rating: ['\u4e70\u5165', '\u589e\u6301', '\u4e2d\u6027'][index % 3],
  url: `https://example.com/report/${index}`,
}));
const searchResults = Array.from({ length: 24 }, (_, index) => ({
  title: `Search result ${index + 1} with a long headline for mobile layout verification`,
  source: 'Primary Research Source',
  from: 'tavily',
  time: '2026-07-28',
  url: `https://example.com/search/${index}`,
  content: 'Long search result content. '.repeat(120),
}));
const graph = {
  ok: true,
  name: 'Concept 1',
  generated_at: '2026-07-28',
  summary: 'A long industry chain summary used to verify readable mobile rendering.',
  segments: [
    { stage: '\u4e0a\u6e38', weight: '30%', items: [{ segment: 'Core materials and high-performance parts', desc: 'Long description for mobile wrapping.', stocks: [{ name: 'Listed Company 1', code: '600001', relation: 'Critical material supplier with high relevance', level: '\u6838\u5fc3' }] }] },
    { stage: '\u4e2d\u6e38', weight: '45%', items: [{ segment: 'Manufacturing and integration', desc: 'System integration and assembly.', stocks: [{ name: 'Listed Company 2', code: '600002', relation: 'System integrator', level: '\u91cd\u8981' }] }] },
    { stage: '\u4e0b\u6e38', weight: '25%', items: [{ segment: 'Operations and services', desc: 'Logistics and city services.', stocks: [{ name: 'Listed Company 3', code: '600003', relation: 'Scenario operator', level: '\u4e00\u822c' }] }] },
  ],
};

function expect(condition, message, details = {}) {
  if (!condition) throw new Error(`${message}\n${JSON.stringify(details, null, 2)}`);
}

async function installMocks(page) {
  await page.route('**/api/chain/concepts**', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ concepts }) }));
  await page.route('**/api/chain/stocks**', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, stocks, graph_exists: true, graph_generating: false, stale: false }) }));
  await page.route('**/api/chain/graph**', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify(graph) }));
  await page.route('**/api/reports/latest**', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, reports }) }));
  await page.route('**/api/search/web**', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, results: searchResults }) }));
  await page.route('**/api/search/stats**', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, tavily: { accounts: [] }, serpapi: { accounts: [] }, twelvedata: { accounts: [] } }) }));
}

async function openResearch(page) {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.addStyleTag({ content: '.auth-overlay{display:none!important}' });
  await page.evaluate(() => {
    document.querySelectorAll('.page.active').forEach(item => item.classList.remove('active'));
    const research = document.getElementById('page-search');
    research.classList.add('active');
    research.style.display = 'flex';
    if (typeof rsInit === 'function') rsInit();
  });
  await page.waitForTimeout(300);
}

async function mobileAudit(browser, viewport, screenshotPrefix) {
  const { width, height } = viewport;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Page.setBypassCSP', { enabled: true });
  await installMocks(page);
  await openResearch(page);

  let state = await page.evaluate(() => {
    const view = document.getElementById('rsView-chain');
    const list = document.querySelector('.research-chain-list-pane');
    const detail = document.querySelector('.research-chain-detail-pane');
    return {
      pane: view?.dataset.mobilePane,
      listDisplay: list ? getComputedStyle(list).display : null,
      detailDisplay: detail ? getComputedStyle(detail).display : null,
      pageOverflowX: document.getElementById('page-search').scrollWidth - document.getElementById('page-search').clientWidth,
    };
  });
  expect(state.pane === 'list', 'Mobile chain view must start in list mode', state);
  expect(state.listDisplay !== 'none' && state.detailDisplay === 'none', 'Only chain list should be visible initially on mobile', state);

  await page.locator('#chainList .srch-result-item').first().click();
  await page.waitForTimeout(250);
  state = await page.evaluate(() => {
    const view = document.getElementById('rsView-chain');
    const list = document.querySelector('.research-chain-list-pane');
    const detail = document.querySelector('.research-chain-detail-pane');
    const back = document.getElementById('chainMobileBack');
    const detailBody = document.getElementById('chainDetail');
    const stockTable = detailBody.querySelector('.chain-stock-table');
    const mobileGraph = detailBody.querySelector('.chain-mobile-graph');
    const desktopGraph = detailBody.querySelector('.chain-desktop-graph');
    const sectionTabs = detailBody.querySelector('.research-chain-section-tabs');
    const graphSection = detailBody.querySelector('[data-chain-section="graph"]');
    const stockSection = detailBody.querySelector('[data-chain-section="stocks"]');
    return {
      pane: view?.dataset.mobilePane,
      listDisplay: list ? getComputedStyle(list).display : null,
      detailDisplay: detail ? getComputedStyle(detail).display : null,
      backDisplay: back ? getComputedStyle(back).display : null,
      detailTop: detail?.getBoundingClientRect().top,
      detailBottom: detail?.getBoundingClientRect().bottom,
      detailOverflowY: getComputedStyle(detailBody).overflowY,
      detailScrollable: detailBody.scrollHeight > detailBody.clientHeight,
      pageOverflowX: document.getElementById('page-search').scrollWidth - document.getElementById('page-search').clientWidth,
      stockOverflowX: stockTable ? stockTable.scrollWidth - stockTable.clientWidth : null,
      mobileGraphDisplay: mobileGraph ? getComputedStyle(mobileGraph).display : null,
      desktopGraphDisplay: desktopGraph ? getComputedStyle(desktopGraph).display : null,
      sectionTabsDisplay: sectionTabs ? getComputedStyle(sectionTabs).display : null,
      graphSectionDisplay: graphSection ? getComputedStyle(graphSection).display : null,
      stockSectionDisplay: stockSection ? getComputedStyle(stockSection).display : null,
    };
  });
  expect(state.pane === 'detail', 'Selecting a concept must switch to detail mode on mobile', state);
  expect(state.listDisplay === 'none' && state.detailDisplay !== 'none', 'Only chain detail should be visible after selection on mobile', state);
  expect(state.backDisplay !== 'none', 'Chain detail must expose a mobile back control', state);
  expect(state.detailTop >= 0 && state.detailTop < height && state.detailBottom <= height + 0.5, 'Chain detail must fit inside the visible research workspace', state);
  expect(state.detailOverflowY === 'auto' && state.detailScrollable, 'Chain detail must own vertical scrolling', state);
  expect(state.pageOverflowX <= 1 && (state.stockOverflowX ?? 0) <= 1, 'Chain content must not create horizontal overflow', state);
  expect(state.mobileGraphDisplay !== 'none' && state.desktopGraphDisplay === 'none', 'Mobile must use the readable chain graph cards', state);
  expect(state.sectionTabsDisplay !== 'none' && state.graphSectionDisplay !== 'none' && state.stockSectionDisplay === 'none', 'Mobile chain detail must expose one graph/stocks section at a time', state);

  await page.locator('[data-chain-section-target="stocks"]').click();
  state = await page.evaluate(() => {
    const detailBody = document.getElementById('chainDetail');
    const graphSection = detailBody.querySelector('[data-chain-section="graph"]');
    const stockSection = detailBody.querySelector('[data-chain-section="stocks"]');
    const stockName = stockSection?.querySelector('.chain-stock-table td:nth-child(2) b');
    return {
      activeSection: detailBody.dataset.mobileSection,
      graphSectionDisplay: graphSection ? getComputedStyle(graphSection).display : null,
      stockSectionDisplay: stockSection ? getComputedStyle(stockSection).display : null,
      stockSectionTop: stockSection?.getBoundingClientRect().top,
      stockNameOverflowX: stockName ? stockName.scrollWidth - stockName.clientWidth : null,
      stockNameWhiteSpace: stockName ? getComputedStyle(stockName).whiteSpace : null,
      pageOverflowX: document.getElementById('page-search').scrollWidth - document.getElementById('page-search').clientWidth,
    };
  });
  expect(state.activeSection === 'stocks' && state.graphSectionDisplay === 'none' && state.stockSectionDisplay !== 'none', 'Mobile chain section switch must reveal stocks without stacking the graph above it', state);
  expect(state.stockSectionTop < 400 && state.pageOverflowX <= 1, 'Mobile stocks must start near the top of the detail workspace', state);
  expect((state.stockNameOverflowX ?? 0) <= 1 && state.stockNameWhiteSpace === 'normal', 'Long stock names must wrap instead of being clipped on narrow phones', state);
  await page.screenshot({ path: path.join(outDir, `${screenshotPrefix}chain-detail.png`), fullPage: false });

  await page.locator('#chainMobileBack').click();
  state = await page.evaluate(() => ({ pane: document.getElementById('rsView-chain').dataset.mobilePane }));
  expect(state.pane === 'list', 'Chain back control must return to the list', state);

  await page.evaluate(() => rsSwitch('report'));
  await page.waitForTimeout(200);
  state = await page.evaluate(() => {
    const view = document.getElementById('rsView-report');
    const body = document.getElementById('repBody');
    const card = body.querySelector('.srch-news-card');
    const filter = view.querySelector('.research-filterbar');
    const titleLink = card?.querySelector('.research-report-link');
    const openAction = card?.querySelector('.research-report-open');
    const meta = card?.querySelector('.research-report-meta');
    return {
      viewBottom: view.getBoundingClientRect().bottom,
      viewHeight: view.getBoundingClientRect().height,
      bodyOverflowY: getComputedStyle(body).overflowY,
      bodyScrollable: body.scrollHeight > body.clientHeight,
      cards: body.querySelectorAll('.srch-news-card').length,
      cardOverflowX: card ? card.scrollWidth - card.clientWidth : 0,
      filterHeight: filter?.getBoundingClientRect().height,
      firstCardTop: card?.getBoundingClientRect().top,
      reportLinkDisplay: titleLink ? getComputedStyle(titleLink).display : null,
      reportActionDisplay: openAction ? getComputedStyle(openAction).display : null,
      reportMetaDisplay: meta ? getComputedStyle(meta).display : null,
      pageOverflowX: document.getElementById('page-search').scrollWidth - document.getElementById('page-search').clientWidth,
    };
  });
  expect(state.viewBottom <= height + 0.5 && state.viewHeight > 0, 'Report view must stay inside the viewport', state);
  expect(state.bodyOverflowY === 'auto' && state.bodyScrollable, 'Report list must own vertical scrolling', state);
  expect(state.cards === reports.length && state.cardOverflowX <= 1 && state.pageOverflowX <= 1, 'Report cards must wrap without horizontal overflow', state);
  expect(state.filterHeight <= 118 && state.firstCardTop < 380, 'Mobile report controls must stay compact enough to expose report content', state);
  expect(state.reportLinkDisplay === 'grid' && state.reportActionDisplay !== 'none' && state.reportMetaDisplay === 'flex', 'Report cards must separate title, reading action, metadata, and tags', state);
  await page.screenshot({ path: path.join(outDir, `${screenshotPrefix}report-list.png`), fullPage: false });

  await page.evaluate(() => rsSwitch('search'));
  await page.fill('#srchInput', 'industry chain');
  await page.evaluate(() => doSearch());
  await page.waitForTimeout(200);
  state = await page.evaluate(() => {
    const view = document.getElementById('rsView-search');
    const results = document.querySelector('.research-search-results-pane');
    const detail = document.querySelector('.research-search-detail-pane');
    return { display: getComputedStyle(view).display, pane: view.dataset.mobilePane, resultsDisplay: results ? getComputedStyle(results).display : null, detailDisplay: detail ? getComputedStyle(detail).display : null };
  });
  expect(state.display === 'flex', 'Search view must use flex, not block', state);
  expect(state.pane === 'results' && state.resultsDisplay !== 'none' && state.detailDisplay === 'none', 'Mobile search must start with results only', state);

  await page.locator('#srchResults .srch-result-item').first().click();
  await page.waitForTimeout(100);
  state = await page.evaluate(() => {
    const view = document.getElementById('rsView-search');
    const detail = document.querySelector('.research-search-detail-pane');
    const body = document.getElementById('srchDetail');
    const back = document.getElementById('searchMobileBack');
    return {
      pane: view.dataset.mobilePane,
      detailDisplay: detail ? getComputedStyle(detail).display : null,
      detailBottom: detail?.getBoundingClientRect().bottom,
      bodyOverflowY: getComputedStyle(body).overflowY,
      bodyScrollable: body.scrollHeight > body.clientHeight,
      backDisplay: back ? getComputedStyle(back).display : null,
      pageOverflowX: document.getElementById('page-search').scrollWidth - document.getElementById('page-search').clientWidth,
    };
  });
  expect(state.pane === 'detail' && state.detailDisplay !== 'none', 'Selecting a search result must switch to search detail mode', state);
  expect(state.backDisplay !== 'none' && state.detailBottom <= height + 0.5, 'Search detail must fit and expose a back control', state);
  expect(state.bodyOverflowY === 'auto' && state.bodyScrollable && state.pageOverflowX <= 1, 'Search detail must scroll without horizontal overflow', state);

  await page.screenshot({ path: path.join(outDir, `${screenshotPrefix}search-detail.png`), fullPage: false });
  await context.close();
}

async function tabletAudit(browser) {
  const context = await browser.newContext({ viewport: { width: 800, height: 900 } });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Page.setBypassCSP', { enabled: true });
  await installMocks(page);
  await openResearch(page);
  const state = await page.evaluate(() => {
    const view = document.getElementById('rsView-chain');
    const list = document.querySelector('.research-chain-list-pane');
    const detail = document.querySelector('.research-chain-detail-pane');
    const lr = list?.getBoundingClientRect();
    const dr = detail?.getBoundingClientRect();
    return {
      direction: getComputedStyle(view).flexDirection,
      listDisplay: list ? getComputedStyle(list).display : null,
      detailDisplay: detail ? getComputedStyle(detail).display : null,
      listWidth: lr?.width,
      detailWidth: dr?.width,
      topDelta: lr && dr ? Math.abs(lr.top - dr.top) : null,
      pageOverflowX: document.getElementById('page-search').scrollWidth - document.getElementById('page-search').clientWidth,
    };
  });
  expect(state.direction === 'row' && state.listDisplay !== 'none' && state.detailDisplay !== 'none', 'Tablet chain view must keep a master-detail split', state);
  expect(state.listWidth >= 250 && state.detailWidth >= 350 && state.topDelta <= 1, 'Tablet chain columns must both remain usable', state);
  expect(state.pageOverflowX <= 1, 'Tablet research page must not overflow horizontally', state);
  await context.close();
}

async function desktopAudit(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Page.setBypassCSP', { enabled: true });
  await installMocks(page);
  await openResearch(page);
  const state = await page.evaluate(() => {
    const view = document.getElementById('rsView-chain');
    const list = document.querySelector('.research-chain-list-pane');
    const detail = document.querySelector('.research-chain-detail-pane');
    return { direction: getComputedStyle(view).flexDirection, listDisplay: list ? getComputedStyle(list).display : null, detailDisplay: detail ? getComputedStyle(detail).display : null };
  });
  expect(state.direction === 'row' && state.listDisplay !== 'none' && state.detailDisplay !== 'none', 'Desktop chain view must retain the two-column layout', state);
  await context.close();
}

const browser = await chromium.launch({ executablePath: chromePath, headless: true });
try {
  await mobileAudit(browser, { width: 320, height: 568 }, 'mobile-320-');
  await mobileAudit(browser, { width: 390, height: 844 }, 'mobile-');
  await tabletAudit(browser);
  await desktopAudit(browser);
  console.log('research mobile layout audit passed');
} finally {
  await browser.close();
}

