/* Real movie router, native forms and local synthetic service responses only.
   Start tests.plus.movie_feedback_fixture:create_app on loopback before running. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const runtime = process.env.OMAKASE_QA_NODE_MODULES || path.join(root, 'node_modules');
const { chromium } = require(require.resolve('playwright', { paths: [runtime] }));
const base = process.env.OMAKASE_MOVIE_QA_BASE || 'http://127.0.0.1:4423';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = process.env.OMAKASE_MOVIE_QA_OUTPUT || path.join(root, '.qa-artifacts/movie-feedback');
const modeFilter = process.env.OMAKASE_MOVIE_QA_MODE;
const actionsOnly = ['actions', 'native-click', 'programmatic'].includes(modeFilter);
fs.mkdirSync(output, { recursive: true });
const receipt = { source: {}, cases: [], errors: [], external: [], limits: 'Actual local HTML routes and native form navigation; synthetic session/service only, cached art, no real providers, playback, acquisition or history writes.' };
for (const name of ['src/omakase/plus/templates/movie_detail.html', 'src/omakase/plus/static/interface.css', 'src/omakase/plus/static/dashboard.js', 'src/omakase/plus/movies_routes.py', 'src/omakase/plus/movie_presentation.py', 'tests/plus/movie_feedback_fixture.py', 'scripts/check_movie_feedback.cjs']) receipt.source[name] = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex');
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true, ignoreDefaultArgs: ['--hide-scrollbars'] });
  try {
    const page = await browser.newPage({ reducedMotion: 'reduce' });
    page.on('pageerror', error => receipt.errors.push(error.message));
    await page.route('**/*', route => {
      if (new URL(route.request().url()).origin !== base) { receipt.external.push(route.request().url()); return route.abort(); }
      return route.continue();
    });
    async function reset(mode, width) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`${base}/qa/reset/${mode}`);
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.locator('.detail-meta .watch-state').innerText(), 'Not watched');
      assert.equal(await page.locator('#movie-rating select').inputValue(), '');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    }
    for (const width of actionsOnly ? [] : [1440, 390, 320]) {
      await reset('stale', width);
      assert.equal(await page.locator('.status-pill').innerText(), 'Status out of date');
      const positions = await page.locator('.movie-detail-body').evaluate(el => {
        const box = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }; };
        return { availability: box('.movie-availability'), rating: box('#movie-rating'), details: box('.movie-verification-details summary'), art: box('.detail-backdrop'), poster: box('.movie-poster') };
      });
      if (width === 1440) {
        assert.ok(Math.abs(positions.availability.top - positions.rating.top) < 2);
        assert.ok(positions.details.bottom < 900, 'Availability details should be visible without a long empty desktop scroll');
      } else assert.ok(positions.rating.top >= positions.availability.bottom);
      await page.screenshot({ path: path.join(output, `movie-${width}-closed.png`), fullPage: true });
      await page.locator('.movie-verification-details summary').click();
      const art = await page.locator('.detail-backdrop').boundingBox();
      assert.equal(art.height, positions.art.bottom - positions.art.top);
      const help = page.locator('.movie-check-help');
      assert.equal(await help.getByRole('link', { name: 'Connections', exact: true }).getAttribute('href'), '/plus/settings/connections');
      assert.equal(await help.getByRole('link', { name: 'Service health', exact: true }).getAttribute('href'), '/plus/health/overview');
      await page.screenshot({ path: path.join(output, `movie-${width}-details.png`), fullPage: true });
      receipt.cases.push({ width, kind: 'layout/disclosure', positions, stableArt: true, historyUnset: true });
    }
    for (const [mode, message, status, width] of [
      ['updated', 'Request checked. The reported state changed.', 'Transfer reported', 1440],
      ['unchanged', 'No change reported.', 'Searching', 390],
      ['saved', 'Request check skipped.', 'Provider restricted', 390],
      ['failed', 'The request check did not complete.', 'Service unavailable', 390],
    ].filter(([mode]) => (modeFilter !== 'native-click' || mode === 'updated') && (modeFilter !== 'programmatic' || mode !== 'updated'))) {
      await reset(mode, width);
      const form = page.locator('form[action$="/refresh"]');
      // Observe the real submit listener before native navigation invalidates
      // the old JavaScript context. The evidence survives in same-origin session
      // storage; no product behavior is replaced or submission prevented here.
      await form.evaluate(el => {
        sessionStorage.removeItem('movie-submit-evidence');
        sessionStorage.removeItem('movie-navigation-evidence');
        const loading = document.getElementById('navigation-loading');
        new MutationObserver(() => {
          if (!loading.hidden) sessionStorage.setItem('movie-navigation-evidence', JSON.stringify({ visible: true, label: loading.querySelector('.navigation-loading-label').textContent }));
        }).observe(loading, { attributes: true, childList: true, subtree: true });
        el.addEventListener('submit', event => {
          const button = el.querySelector('button');
          const rows = JSON.parse(sessionStorage.getItem('movie-submit-evidence') || '[]');
          rows.push({ text: button.textContent, disabled: button.disabled, busy: button.getAttribute('aria-busy'), spinner: getComputedStyle(button, '::before').content, prevented: event.defaultPrevented });
          sessionStorage.setItem('movie-submit-evidence', JSON.stringify(rows));
        });
      });
      const cdp = await page.context().newCDPSession(page);
      const activation = mode === 'updated'
        ? form.locator('button').click({ noWaitAfter: true })
        : page.evaluate(() => { setTimeout(() => { const el = document.querySelector('form[action$="/refresh"]'); const button = el.querySelector('button'); el.requestSubmit(button); el.requestSubmit(button); }, 50); });
      for (let attempt = 0; ; attempt++) {
        const pending = await (await fetch(`${base}/qa/receipt`)).json();
        if (pending.checks === 1) break;
        assert.ok(attempt < 80, 'Synthetic native POST did not start');
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      await new Promise(resolve => setTimeout(resolve, 220));
      const screen = await cdp.send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(output, `movie-${mode}-busy.png`), Buffer.from(screen.data, 'base64'));
      await fetch(`${base}/qa/complete`);
      await page.waitForURL(`**?notice=request_${mode}`);
      await activation; await cdp.detach();
      const submissions = await page.evaluate(() => JSON.parse(sessionStorage.getItem('movie-submit-evidence')));
      const navigation = await page.evaluate(() => JSON.parse(sessionStorage.getItem('movie-navigation-evidence')));
      assert.deepEqual(navigation, { visible: true, label: 'Checking request…' });
      const busy = submissions[0];
      assert.equal(busy.text, 'Checking request…'); assert.equal(busy.disabled, true); assert.equal(busy.busy, 'true');
      assert.equal(busy.prevented, false); assert.notEqual(busy.spinner, 'none');
      if (mode !== 'updated') assert.equal(submissions[1].prevented, true);
      assert.ok((await page.locator('.product-notice').innerText()).startsWith(message));
      assert.equal(await page.locator('.status-pill').innerText(), status);
      assert.equal(await page.locator('form[action$="/refresh"] button').isDisabled(), false);
      const counts = await (await page.request.get(`${base}/qa/receipt`)).json();
      assert.equal(counts.checks, 1); assert.equal(counts.history_rows, 0); assert.equal(counts.acquisitions, 0); assert.equal(counts.media_probes, 0);
      await page.screenshot({ path: path.join(output, `movie-${mode}-result.png`), fullPage: true });
      receipt.cases.push({ kind: 'native POST/303/GET', mode, width, trigger: mode === 'updated' ? 'actual button click' : 'double requestSubmit', busy, navigation, submissions, counts, status });
    }
    for (const [mode, expected] of ['native-click', 'programmatic'].includes(modeFilter) ? [] : [['ready', 'Ready in Plex'], ['unknown', 'Status unknown'], ['no_result', 'No eligible copy']]) {
      await reset(mode, 390);
      assert.equal(await page.locator('.status-pill').innerText(), expected);
      if (mode === 'ready') assert.equal(await page.getByRole('link', { name: 'Open in Plex', exact: true }).count(), 1);
      else assert.equal(await page.getByRole('link', { name: 'Open in Plex', exact: true }).count(), 0);
      receipt.cases.push({ kind: 'distinct idle status', mode, expected, historyUnset: true });
    }
    assert.deepEqual(receipt.errors, []); assert.deepEqual(receipt.external, []);
    receipt.status = 'PASS';
  } catch (error) { receipt.status = 'FAIL'; receipt.failure = error.stack; process.exitCode = 1; }
  finally { await browser.close(); fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n'); }
  console.log(JSON.stringify({ status: receipt.status, cases: receipt.cases.length, failure: receipt.failure, output }));
})();
