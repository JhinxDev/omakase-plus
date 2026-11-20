/* Native Windows Edge scrollbars and project links; no provider or account requests. */
const { qaRuntime, qaPython } = require('./qa-environment.cjs');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
// Pinned browser on purpose: this check measures native Windows scrollbar
// rendering, so it needs the installed msedge channel and must keep the default
// --hide-scrollbars argument removed. qaBrowser() cannot express that, and an
// executable override would silently change what is being measured. The entry is
// registered in scripts/qa-environment.test.cjs as a documented exception.
const runtime = qaRuntime();
const { chromium } = require(require.resolve('playwright', { paths: [runtime] }));
const output = process.env.OMAKASE_INTERFACE_QA_OUTPUT || path.join(root, '.qa-artifacts/interface');
fs.mkdirSync(output, { recursive: true });
const html = execFileSync(qaPython(), ['-c', "import runpy; f=runpy.run_path('tests/plus/test_global_search_ui.py'); print(f['render_search'](kind='anime',query=''))"], { cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
const source = {};
for (const file of ['static/interface.css', 'static/search.js', 'static/product-shell.css', 'static/dashboard.js', 'templates/base.html']) {
  source[file] = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'src/omakase/plus', file))).digest('hex');
}
const receipt = { source, checks: [], writes: [], errors: [], engine: 'Windows Edge, headless; only --hide-scrollbars default removed', limitations: ['Intercepted title fixtures; emulated narrow viewports and forced colors, not physical device acceptance. Project destinations are checked without visiting public URLs.'] };
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
      assert.equal(await list.isVisible(), false, 'Escape must hide search suggestions');
      const desktop = width === 1440;
      const accountToggle = page.locator('[data-account-menu] > summary');
      const project = page.locator(desktop ? '.app-sidebar-project' : '.mobile-project-links');
      const otherProject = page.locator(desktop ? '.mobile-project-links' : '.app-sidebar-project');
      assert.equal(await otherProject.isVisible(), false, 'Only the viewport-appropriate Project links should be visible');
      if (!desktop) {
        assert.equal(await project.isVisible(), false, 'Closed account menu hides mobile project links');
        await accountToggle.press('Enter');
      }
      await project.waitFor({ state: 'visible', timeout: 4000 });
      assert.equal(await project.getAttribute('aria-label'), 'Project links');
      const links = [];
      for (const [name, href] of [['Documentation', 'https://omakase-plus.jhinx.dev/docs'], ['GitHub', 'https://github.com/JhinxDev/omakase-plus'], ['Made by Jhinx Dev', 'https://jhinx.dev']]) {
        const link = project.getByRole('link', { name, exact: true });
        assert.equal(await link.getAttribute('href'), href);
        assert.equal(await link.getAttribute('target'), '_blank');
        const rel = (await link.getAttribute('rel')).split(/\s+/);
        assert(rel.includes('noopener') && rel.includes('noreferrer'));
        await link.focus();
        assert(await link.evaluate(el => el === document.activeElement && el.matches(':focus-visible')), name + ' must have keyboard focus');
        const bounds = await link.boundingBox();
        assert(bounds && bounds.height >= 44 && bounds.width >= 44, name + ' has a 44px target');
        assert(bounds.x >= 0 && bounds.x + bounds.width <= width + 1, name + ' fits in the viewport');
        links.push({ name, bounds });
      }
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Project links must not overflow the page');
      await page.screenshot({ path: path.join(output, `project-links-${width}.png`) });
      if (!desktop) {
        await page.keyboard.press('Escape');
        assert.equal(await project.isVisible(), false, 'Escape closes the mobile project links');
        assert(await accountToggle.evaluate(el => el === document.activeElement), 'Escape restores focus to account toggle');
      }
      receipt.checks.push({ width, native, wheel: true, keyboard: true, forcedColors: true,
        publicLinks: { placement: desktop ? 'sidebar' : 'account menu', links, escapeRestoresFocus: !desktop } });
    }
    assert.deepEqual(receipt.errors, []); assert.deepEqual(receipt.writes, []);
    receipt.status = 'PASS';
  } catch (error) { receipt.status = 'FAIL'; receipt.error = error.message; throw error; }
  finally { await browser.close(); fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2)); }
  console.log(JSON.stringify({ status: receipt.status, cases: receipt.checks.length, output }));
})().catch(error => { console.error(error); process.exitCode = 1; });
