/* Saved-position startup against the isolated growing-playlist fixture. */
const assert = require('node:assert/strict');
const path = require('node:path');
const { test } = require('node:test');
const { qaRuntime, qaBrowser } = require('../../scripts/qa-environment.cjs');
const { chromium } = require(path.join(qaRuntime(), 'playwright'));

test('a growing stream plays and saves only after reaching its saved position', { timeout: 45000 }, async () => {
  const base = new URL(process.env.OMAKASE_PLAYER_FIXTURE_URL || 'http://127.0.0.1:8828');
  assert.equal(base.hostname, '127.0.0.1');
  const browser = await chromium.launch({ ...qaBrowser(), args: ['--mute-audio'] });
  try {
    const page = await browser.newPage();
    const events = [];
    let firstPlaylistAt = 0, limited = 0, full = 0, injectedResume = false;

    page.on('response', response => {
      if (response.url().endsWith('/events')) {
        events.push({ status: response.status(), body: response.request().postDataJSON() });
      }
    });
    await page.route('**/plus/static/web-player.js?*', route => route.fulfill({
      path: path.join(__dirname, '../../src/omakase/plus/static/web-player.js'),
      contentType: 'application/javascript',
    }));
    await page.route(/\/api\/playback\/sessions(?:\/[^/]+\/prepare)?$/, async route => {
      const response = await route.fetch();
      const session = await response.json();
      if (!session.streaming || session.preparation_required) return route.fulfill({ response });
      injectedResume = true;
      return route.fulfill({
        response, json: { ...session, resume_version_changed: false, position_seconds: 300 },
      });
    });
    await page.route('**/playlist.m3u8', async route => {
      const response = await route.fetch();
      const text = await response.text();
      firstPlaylistAt ||= Date.now();
      const lines = text.split('\n');
      const prefix = lines.findIndex(line => line.endsWith('seg-00007.ts'));
      if (Date.now() - firstPlaylistAt < 9000 && prefix >= 0) {
        limited++;
        return route.fulfill({ response, body: lines.slice(0, prefix + 1).join('\n') + '\n' });
      }
      full++;
      return route.fulfill({ response });
    });

    await page.goto(new URL('/review/sign-in', base).href);
    const video = page.locator('[data-player-video]');
    await video.evaluate(node => { node.muted = true; });
    assert.equal(await page.locator('[data-player-resume]').isChecked(), true);
    await page.locator('[data-player-start]').click();
    await page.waitForFunction(() => {
      const video = document.querySelector('[data-player-video]');
      return !video.paused || document.querySelector('[data-player-status]').textContent.includes('Preparing your saved position');
    }, null, { timeout: 22000 });
    assert.ok(injectedResume && limited >= 1, 'The fixture must provide a resume beyond the first playlist');
    assert.equal(await video.evaluate(node => node.paused), true, 'Playback began before the saved position was reachable');
    assert.equal(events.length, 0, 'Startup must not overwrite the stored resume while waiting');

    await page.waitForFunction(() => {
      const video = document.querySelector('[data-player-video]');
      return !video.paused && video.currentTime >= 299 && video.getVideoPlaybackQuality().totalVideoFrames > 4;
    }, null, { timeout: 22000 });
    await page.getByText('Progress saved.', { exact: true }).waitFor();
    assert.ok(full >= 1);
    assert.ok(events.length && events.every(({ status, body }) => status === 200 && body.position_seconds >= 299));
    assert.ok(await page.locator('[data-player-error]').isHidden());
  } finally {
    await browser.close();
  }
});
