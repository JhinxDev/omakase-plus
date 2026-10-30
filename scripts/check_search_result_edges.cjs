/* Real native Edge scrolling: mixed result lengths, both ends and high contrast. */
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const runtime = process.env.OMAKASE_QA_NODE_MODULES || path.join(root, 'node_modules');
const { chromium } = require(require.resolve('playwright', { paths: [runtime] }));
const output = process.env.OMAKASE_SEARCH_EDGE_OUTPUT || path.join(root, '.qa-artifacts/search-edges');
const poster = process.env.OMAKASE_SEARCH_EDGE_POSTER;
if (poster) assert(fs.statSync(poster).isFile(), 'Use an existing cached poster fixture');
fs.mkdirSync(output, { recursive: true });
const html = execFileSync(process.env.OMAKASE_QA_PYTHON || path.join(root, '.venv/Scripts/python.exe'), ['-c', "import runpy; f=runpy.run_path('tests/plus/test_global_search_ui.py'); print(f['render_search'](kind='anime',query=''))"], { cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
const titles = ['Solo Adventurer', 'The Very Long Journey Beyond Every Mountain and Across the Distant Kingdoms', 'A Quiet Film', 'A Story Told Across Three Different Generations', 'Summer', 'The Return of the Travelers Who Thought Their Journey Had Already Ended', 'A Short Story', 'The Last Adventure'];
const cards = titles.map((title, index) => ({ title, media_type: 'anime', year: 2020 + index, format: index % 2 ? 'TV special feature' : 'TV', library_id: index % 2 ? 7 : null, watch_status: index % 3 ? 'watching' : 'completed', rating: index % 2 ? 8.5 : null, url: `/plus/catalog/anime/${index + 1}` }));
if (poster) cards.forEach(card => { card.poster_url = '/plus/demo-poster.jpg'; });
const receipt = { status: 'RUNNING', source: {}, checks: [], errors: [], writes: [], limitations: ['Synthetic intercepted metadata, actual Windows Edge rendering/native scrolling; emulated narrow viewports and forced colors. No provider or account mutations.'] };
for (const relative of ['src/omakase/plus/static/search.js', 'src/omakase/plus/static/interface.css', 'src/omakase/plus/templates/base.html', 'src/omakase/plus/templates/search.html', 'scripts/check_search_result_edges.cjs']) receipt.source[relative] = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, relative))).digest('hex');
const geometry = async list => list.evaluate(el => {
  const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
  const top = rect.top + el.clientTop + parseFloat(style.paddingTop), bottom = rect.top + el.clientTop + el.clientHeight - parseFloat(style.paddingBottom);
  return {
    top: rect.top, bottom: rect.bottom, viewport: innerHeight, scrollTop: el.scrollTop, maxScroll: el.scrollHeight - el.clientHeight,
    rows: [...el.querySelectorAll('[role=option]')].map(option => {
      const row = option.getBoundingClientRect(), copy = option.querySelector('span');
      const parts = [...option.children].map(child => child.getBoundingClientRect());
      const inkTop = Math.min(...parts.map(part => part.top)), inkBottom = Math.max(...parts.map(part => part.bottom));
      return { id: option.id, intersects: inkBottom > top && inkTop < bottom, full: inkTop >= top - 1 && inkBottom <= bottom + 1, outerPaddingFull: row.top >= top - 1 && row.bottom <= bottom + 1, inkTop, inkBottom, copyVisible: Number(getComputedStyle(copy).opacity) * Number(getComputedStyle(option).opacity) > .01, posterVisible: !!option.querySelector('img') && Number(getComputedStyle(option).opacity) > .01, metadata: option.querySelector('small').textContent, label: option.querySelector('strong').textContent, selected: option.getAttribute('aria-selected') === 'true', outline: getComputedStyle(option).outlineStyle, outlineWidth: getComputedStyle(option).outlineWidth };
    })
  };
});
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true, ignoreDefaultArgs: ['--hide-scrollbars'] });
  try {
    const page = await browser.newPage({ reducedMotion: 'reduce' });
    page.on('pageerror', error => receipt.errors.push(error.message));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (request.method() !== 'GET') { receipt.writes.push(url.pathname); return route.abort(); }
      if (url.origin !== 'http://search-edges.test') return route.abort();
      if (url.pathname === '/plus/search') return route.fulfill({ contentType: 'text/html', body: html });
      if (url.pathname === '/plus/search/suggestions') return route.fulfill({ json: { results: cards, warnings: [] } });
      if (poster && url.pathname === '/plus/demo-poster.jpg') return route.fulfill({ path: poster });
      if (url.pathname.startsWith('/plus/static/')) {
        const file = path.resolve(root, 'src/omakase/plus/static', url.pathname.slice('/plus/static/'.length));
        if (file.startsWith(path.join(root, 'src/omakase/plus/static') + path.sep) && fs.existsSync(file)) return route.fulfill({ path: file });
      }
      if (/^\/plus\/catalog\/anime\/[1-8]$/.test(url.pathname)) return route.fulfill({ contentType: 'text/html', body: '<h1>Selected exact fixture title</h1>' });
      return route.fulfill({ status: 404, body: '' });
    });
    for (const width of poster ? [320] : [1440, 390, 320]) for (const forcedColors of ['none', 'active']) {
      await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ forcedColors });
      await page.goto('http://search-edges.test/plus/search'); await page.evaluate(() => document.fonts.ready);
      const form = page.locator(width === 1440 ? '.header-search' : '.catalog-search-form');
      const input = form.locator('[data-search-input]'), list = form.locator('[data-search-suggestions]');
      await input.fill('solo'); await list.getByRole('option').nth(7).waitFor({ state: 'visible' });
      if (poster) await list.locator('img').evaluateAll(images => Promise.all(images.map(image => image.decode())));
      const states = [];
      const wheelSeen = new Set();
      receipt.activeCase = { width, forcedColors, nativeWheelStates: states, wheelReadableIds: [] };
      const deltas = poster ? [0, 32, ...Array(40).fill(40)] : [0, 32, 94, 210, 10000];
      for (const delta of deltas) {
        if (delta) { await list.hover(); await page.mouse.wheel(0, delta); await page.waitForTimeout(120); }
        await page.evaluate(() => new Promise(requestAnimationFrame));
        const state = await geometry(list); states.push(state);
        assert.equal(state.rows.length, 8, 'Every accessible option remains present');
        assert(state.rows.every(row => row.metadata && row.label), 'Metadata and full titles remain in each actual result');
        assert(!state.rows.some(row => row.intersects && !row.full && row.copyVisible), 'A clipped result fragment must not expose a detached subtitle');
        assert(!state.rows.some(row => row.intersects && !row.full && row.posterVisible), 'A clipped result must not expose an isolated poster');
        assert(state.rows.some(row => row.full && row.copyVisible), 'At least one complete readable result remains');
        state.rows.filter(row => row.full && row.copyVisible).forEach(row => wheelSeen.add(row.id));
        receipt.activeCase.wheelReadableIds = [...wheelSeen];
        assert(state.top >= 0 && state.bottom <= state.viewport - 12, 'Popup fits the available viewport');
        if (poster && states.length === 2) await page.screenshot({ path: path.join(output, `near-top-${width}-${forcedColors}.png`) });
        if (poster && delta && Math.abs(state.scrollTop - state.maxScroll) <= .1) break;
      }
      assert(Math.abs(states.at(-1).scrollTop - states.at(-1).maxScroll) <= .1, 'Native wheel reaches actual maximum scroll');
      if (poster) assert.equal(wheelSeen.size, 8, 'Every result becomes completely readable by native wheel alone');
      await page.screenshot({ path: path.join(output, `maximum-${width}-${forcedColors}.png`) });
      const selectedRows = [];
      for (let index = 0; index < 8; index++) {
        await input.press('ArrowDown'); await page.evaluate(() => new Promise(requestAnimationFrame));
        const state = await geometry(list), selected = state.rows.find(row => row.selected);
        assert(selected && selected.full && selected.copyVisible, 'Keyboard selection reveals a complete result and its metadata');
        if (forcedColors === 'active') assert(selected.outline === 'solid' && selected.outlineWidth === '2px', 'Active result remains visibly indicated in forced colors');
        selectedRows.push(selected.id);
      }
      assert.equal(new Set(selectedRows).size, 8, 'All eight actual results are keyboard reachable');
      await page.screenshot({ path: path.join(output, `selected-${width}-${forcedColors}.png`) });
      const last = list.getByRole('option').nth(7); await last.click(); await page.waitForURL('**/plus/catalog/anime/8');
      receipt.checks.push({ width, forcedColors, cachedPosters: !!poster, nativeWheelStates: states, wheelReadableIds: [...wheelSeen], allEightKeyboardReachable: true, pointerSelection: true, selectedRows });
    }
    assert.deepEqual(receipt.errors, []); assert.deepEqual(receipt.writes, []); receipt.status = 'PASS';
  } catch (error) { receipt.status = 'FAIL'; receipt.error = error.message; throw error; }
  finally { await browser.close(); fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2)); }
  console.log(JSON.stringify({ status: receipt.status, cases: receipt.checks.length, output }));
})().catch(error => { console.error(error); process.exitCode = 1; });
