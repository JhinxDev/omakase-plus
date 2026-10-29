/* Optional contributor QA: read-only local showcase, Playwright in OMAKASE_QA_RUNTIME. */
'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {chromium} = require(require.resolve('playwright', {paths: [process.env.OMAKASE_QA_RUNTIME || process.cwd()]}));
const origin = new URL(process.env.OMAKASE_QA_ORIGIN || 'http://127.0.0.1:4419');
assert(['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname), 'Use a local read-only showcase');
const out = process.env.OMAKASE_QA_OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'omakase-settings-nav-'));
if (process.env.OMAKASE_QA_OUT) fs.mkdirSync(out);
const root = path.resolve(__dirname, '..');
const receipt = {status: 'RUNNING', origin: origin.origin, cases: [], source: {}, blocked: [], pageErrors: [], httpErrors: [], limits: ['Synthetic read-only local showcase in headless Edge; no physical-device or playback acceptance.']};
for (const name of ['src/omakase/plus/templates/_settings_nav.html', 'src/omakase/plus/static/refinement.css', 'src/omakase/plus/static/shelves.js']) receipt.source[name] = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex');
function allowed(raw, method) {
  const u = new URL(raw);
  return method === 'GET' && u.origin === origin.origin && (['/plus/settings/preferences', '/plus/auth/google/account', '/plus/settings/storage', '/plus/api/notifications', '/plus/account/avatar', '/favicon.ico'].includes(u.pathname) || /^\/plus\/static\//.test(u.pathname));
}
async function focusedGeometry(page, link, nav, constrained) {
  await page.waitForFunction(({element, region, bounded}) => {
    const r = element.getBoundingClientRect(), n = region.getBoundingClientRect(), s = getComputedStyle(element), reach = parseFloat(s.outlineWidth) + parseFloat(s.outlineOffset);
    return element === document.activeElement && element.matches(':focus-visible') && (!bounded || (r.left - reach >= n.left - 0.8 && r.right + reach <= n.right + 0.8));
  }, {element: await link.elementHandle(), region: await nav.elementHandle(), bounded: constrained}, {timeout: 1500});
  return link.evaluate(e => {
    const r = e.getBoundingClientRect(), n = e.closest('nav').getBoundingClientRect(), s = getComputedStyle(e), outlineWidth = parseFloat(s.outlineWidth), outlineOffset = parseFloat(s.outlineOffset);
    return {href: e.getAttribute('href'), focusVisible: e.matches(':focus-visible'), outlineWidth, outlineOffset, outlineStyle: s.outlineStyle, left: r.left, right: r.right, outlineLeft: r.left - outlineWidth - outlineOffset, outlineRight: r.right + outlineWidth + outlineOffset, viewportLeft: n.left, viewportRight: n.right};
  });
}
(async () => {
  let browser;
  try {
    browser = await chromium.launch({channel: 'msedge', headless: true});
    for (const viewportWidth of [1440, 390, 320]) {
      const context = await browser.newContext({viewport: {width: viewportWidth, height: 844}, hasTouch: viewportWidth < 600, reducedMotion: 'reduce', serviceWorkers: 'block'});
      await context.route('**/*', route => {
        if (!allowed(route.request().url(), route.request().method())) {
          receipt.blocked.push({path: new URL(route.request().url()).pathname, method: route.request().method()});
          return route.abort();
        }
        return route.continue();
      });
      const page = await context.newPage();
      page.on('pageerror', error => receipt.pageErrors.push(error.name));
      page.on('response', response => {if (response.status() >= 400) receipt.httpErrors.push({path: new URL(response.url()).pathname, status: response.status()});});
      const response = await page.goto(origin.origin + '/plus/settings/preferences', {waitUntil: 'networkidle'});
      assert.equal(response.status(), 200);
      const nav = page.getByRole('navigation', {name: 'Settings navigation', exact: true}), links = nav.locator('a'), count = await links.count();
      assert.equal(await nav.locator('a[aria-current="page"]').getAttribute('href'), '/plus/settings/preferences');
      assert.equal(await nav.locator('h2[tabindex], h2 a, h2 button').count(), 0);
      assert(count >= 13);
      await links.first().focus(); await page.keyboard.press('Tab');
      const inactive = links.nth(1);
      assert.equal(await inactive.getAttribute('aria-current'), null);
      const item = {viewportWidth, inactive: await focusedGeometry(page, inactive, nav, viewportWidth < 600), endpoints: []};
      receipt.cases.push(item);
      assert(item.inactive.outlineWidth >= 2 && item.inactive.outlineStyle === 'solid');
      await page.screenshot({path: path.join(out, `inactive-${viewportWidth}.png`)});
      if (viewportWidth < 600) {
        for (const end of ['first', 'last']) {
          if (end === 'first') {await links.nth(1).focus(); await page.keyboard.press('Shift+Tab');}
          else {await links.nth(count - 2).focus(); await page.keyboard.press('Tab');}
          const link = end === 'first' ? links.first() : links.last();
          item.endpoints.push({end, ...await focusedGeometry(page, link, nav, true)});
          await page.screenshot({path: path.join(out, `${end}-${viewportWidth}.png`)});
        }
      }
      const middle = nav.locator('a[href="/plus/settings/storage"]');
      await nav.locator('a[href="/plus/settings/connections"]').focus();
      await page.keyboard.press('Tab');
      item.middle = await focusedGeometry(page, middle, nav, viewportWidth < 600);
      const destination = await middle.getAttribute('href');
      await page.keyboard.press('Enter'); await page.waitForURL(origin.origin + destination);
      assert.equal(await page.locator('.settings-navigation a[aria-current="page"]').getAttribute('href'), destination);
      item.enterDestination = destination;
      item.noPageOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1);
      assert(item.noPageOverflow);
      await context.close();
    }
    assert.deepEqual(receipt.blocked, []); assert.deepEqual(receipt.pageErrors, []); assert.deepEqual(receipt.httpErrors, []);
    receipt.status = 'PASS';
  } catch (error) {
    receipt.status = 'FAIL'; receipt.failure = {name: error.name, message: error.message, stack: error.stack};
  } finally {
    if (browser) await browser.close();
    fs.writeFileSync(path.join(out, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
    console.log(JSON.stringify({status: receipt.status, cases: receipt.cases.length, receipt: path.join(out, 'receipt.json')}));
    if (receipt.status !== 'PASS') process.exitCode = 1;
  }
})();
