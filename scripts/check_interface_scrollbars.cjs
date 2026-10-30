/* Native Windows Edge scrollbars and footer; no provider or account requests. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const runtime = process.env.OMAKASE_QA_NODE_MODULES || path.join(root, 'node_modules');
const { chromium } = require(require.resolve('playwright', { paths: [runtime] }));
const output = process.env.OMAKASE_INTERFACE_QA_OUTPUT || path.join(root, '.qa-artifacts/interface');
fs.mkdirSync(output, { recursive: true });
const html = execFileSync(process.env.OMAKASE_QA_PYTHON || path.join(root, '.venv/Scripts/python.exe'), ['-c', "import runpy; f=runpy.run_path('tests/plus/test_global_search_ui.py'); print(f['render_search'](kind='anime',query=''))"], { cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
const source = {};
for (const name of ['interface.css', 'search.js']) source[name] = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'src/omakase/plus/static', name))).digest('hex');
const receipt = { source, checks: [], writes: [], errors: [], engine: 'Windows Edge, headless; only --hide-scrollbars default removed', limitations: ['Intercepted title fixtures; emulated narrow viewports and forced colors, not physical device acceptance. Footer destinations separately verified public.'] };
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true, ignoreDefaultArgs: ['--hide-scrollbars'] });
  try {
    const page = await browser.newPage({ reducedMotion: 'reduce' });
    page.on('pageerror', error => receipt.errors.push(error.message));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (request.method() !== 'GET') { receipt.writes.push(url.pathname); return route.abort(); }
      if (url.origin !== 'http://interface.test') return route.abort();
      if (url.pathname === '/plus/search/suggestions') return route.fulfill({ json: { results: Array.from({ length: 8 }, (_, index) => ({ title: `Solo Adventurer ${index + 1}`, media_type: 'anime', url: `/plus/catalog/anime/${index + 1}` })), warnings: [] } });
      if (url.pathname === '/plus/search') return route.fulfill({ contentType: 'text/html', body: html });
      if (url.pathname.startsWith('/plus/static/')) {
        const file = path.resolve(root, 'src/omakase/plus/static', url.pathname.slice('/plus/static/'.length));
        if (file.startsWith(path.join(root, 'src/omakase/plus/static') + path.sep) && fs.existsSync(file)) return route.fulfill({ path: file });
      }
      return route.fulfill({ status: 404, body: '' });
    });
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('http://interface.test/plus/search');
      await page.evaluate(() => document.fonts.ready);
      const form = page.locator(width === 1440 ? '.header-search' : '.catalog-search-form');
      const input = form.locator('[data-search-input]'), list = form.locator('[data-search-suggestions]');
      await input.fill('solo');
      await list.getByRole('option').nth(7).waitFor({ state: 'visible' });
      const native = await list.evaluate(el => ({ ownerWidth: getComputedStyle(el).scrollbarWidth, ownerColor: getComputedStyle(el).scrollbarColor, width: getComputedStyle(el, '::-webkit-scrollbar').width, buttons: getComputedStyle(el, '::-webkit-scrollbar-button').display, buttonHeight: getComputedStyle(el, '::-webkit-scrollbar-button').height, thumb: getComputedStyle(el, '::-webkit-scrollbar-thumb').backgroundColor, scrollable: el.scrollHeight > el.clientHeight }));
      assert.deepEqual([native.ownerWidth, native.ownerColor, native.width, native.buttons, native.buttonHeight, native.scrollable], ['auto', 'auto', '8px', 'none', '0px', true]);
      await list.hover(); await page.mouse.wheel(0, 180);
      await page.waitForFunction(selector => document.querySelector(selector).scrollTop > 0, width === 1440 ? '.header-search [data-search-suggestions]' : '.catalog-search-form [data-search-suggestions]');
      await input.press('ArrowUp');
      assert.equal(await input.getAttribute('aria-activedescendant'), await list.getByRole('option').nth(7).getAttribute('id'));
      await page.screenshot({ path: path.join(output, `native-scrollbar-${width}.png`) });
      await page.emulateMedia({ forcedColors: 'active' });
      assert.equal(await list.evaluate(el => getComputedStyle(el, '::-webkit-scrollbar-button').display), 'none');
      await page.screenshot({ path: path.join(output, `forced-colors-${width}.png`) });
      await page.emulateMedia({ forcedColors: 'none' }); await input.press('Escape');
      const footer = page.locator('.product-footer'); await footer.scrollIntoViewIfNeeded();
      const bounds = await footer.evaluate(el => ({ left: el.querySelector('.footer-identity > span').getBoundingClientRect().left, right: Math.max(...[...el.querySelectorAll('a')].map(link => link.getBoundingClientRect().right)), gutter: parseFloat(getComputedStyle(el).paddingLeft), pageOverflow: document.documentElement.scrollWidth > innerWidth + 1 }));
      assert(bounds.left >= 16 && bounds.right <= width - 16 && !bounds.pageOverflow);
      assert(Math.abs(bounds.left - bounds.gutter) <= 1, 'Footer text uses the shared page gutter');
      for (const [name, href] of [['Documentation', 'https://omakase-plus.jhinx.dev/docs'], ['GitHub', 'https://github.com/JhinxDev/omakase-plus'], ['Made by Jhinx Dev', 'https://jhinx.dev']]) {
        const link = footer.getByRole('link', { name }); assert.equal(await link.getAttribute('href'), href); assert.equal(await link.getAttribute('target'), '_blank');
        assert((await link.getAttribute('rel')).includes('noopener')); assert((await link.boundingBox()).height >= 44);
        await link.focus(); assert(await link.evaluate(el => el.matches(':focus-visible')));
      }
      await page.screenshot({ path: path.join(output, `footer-${width}.png`) });
      receipt.checks.push({ width, native, wheel: true, keyboard: true, forcedColors: true, footer: bounds, publicLinks: true });
    }
    assert.deepEqual(receipt.errors, []); assert.deepEqual(receipt.writes, []);
    receipt.status = 'PASS';
  } catch (error) { receipt.status = 'FAIL'; receipt.error = error.message; throw error; }
  finally { await browser.close(); fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2)); }
  console.log(JSON.stringify({ status: receipt.status, cases: receipt.checks.length, output }));
})().catch(error => { console.error(error); process.exitCode = 1; });
