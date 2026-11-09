/* Verify explicit play startup on a synthetic loopback clip. */
const assert = require('node:assert/strict');
const path = require('node:path');
const {test} = require('node:test');
const {qaRuntime, qaBrowser} = require('../../scripts/qa-environment.cjs');
const {chromium} = require(path.join(qaRuntime(), 'playwright'));

for (const {width, blockAutoplay} of [
  {width: 1280, blockAutoplay: false},
  {width: 390, blockAutoplay: true},
]) {
  test(`explicit play startup at ${width}px`, {timeout: 30000}, async () => {
    const base = new URL(process.env.OMAKASE_PLAYER_REVIEW_URL || 'http://127.0.0.1:8126');
    assert.equal(base.hostname, '127.0.0.1');
    const browser = await chromium.launch({...qaBrowser(), args: ['--mute-audio']});
    try {
      const page = await browser.newPage({viewport: {width, height: 900}, reducedMotion: 'reduce'});
      await page.addInitScript(block => {
        const native = HTMLMediaElement.prototype.play;
        HTMLMediaElement.prototype.play = function(...args) {
          if (block) {
            block = false;
            return Promise.reject(new DOMException('Fixture denied autoplay', 'NotAllowedError'));
          }
          this.muted = true;
          return native.apply(this, args);
        };
      }, blockAutoplay);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let starts = 0;
      let allowStart;
      const gated = new Promise(resolve => { allowStart = resolve; });
      await page.route('**/api/playback/sessions', async route => {
        starts++;
        await gated;
        await route.continue();
      });

      await page.goto(new URL('/review/sign-in', base).href);
      await page.goto(new URL('/plus/watch/1/1?play=1', base).href);
      const stage = page.locator('[data-player-container]');
      const loading = page.locator('[data-player-loading]');
      const startPanel = page.locator('[data-player-start-panel]');
      const video = page.locator('[data-player-video]');
      assert.ok(await loading.isVisible());
      assert.ok(await startPanel.isHidden());
      assert.equal(await stage.getAttribute('aria-busy'), 'true');
      assert.equal(await loading.evaluate(node => getComputedStyle(node).backgroundColor), 'rgb(2, 4, 8)');
      const before = await video.boundingBox();
      assert.ok(Math.abs(before.width / before.height - 16 / 9) < 0.02);
      const motion = await loading.locator('.web-player-loading-ring').evaluate(node => getComputedStyle(node).animationName);
      assert.equal(motion, 'none');
      allowStart();

      if (blockAutoplay) {
        await page.getByText('Press Play to start', {exact: true}).waitFor();
        assert.ok(await loading.isHidden());
        await page.locator('[data-player-start]').click();
      }
      await page.waitForFunction(() => {
        const media = document.querySelector('[data-player-video]');
        return !media.paused && media.currentTime > 0.2 && media.getVideoPlaybackQuality().totalVideoFrames > 2;
      });
      assert.equal(starts, 1);
      assert.ok(await loading.isHidden());
      assert.ok(await startPanel.isHidden());
      const after = await video.boundingBox();
      assert.ok(Math.abs(before.height - after.height) < 1);

      const timeline = page.locator('[data-player-timeline]');
      await timeline.click({position: {x: Math.round(after.width / 4), y: 15}});
      assert.equal(await timeline.evaluate(node => getComputedStyle(node).outlineStyle), 'none');
      await page.keyboard.press('Tab');
      await page.keyboard.press('Shift+Tab');
      assert.ok(await timeline.evaluate(node => node.matches(':focus-visible')));
      assert.deepEqual(errors, []);
    } finally {
      await browser.close();
    }
  });
}

for (const title of ['movie', 'tv']) {
  test(`the shared player plays a ${title} and saves a seek`, {timeout: 30000}, async () => {
    const base = new URL(process.env.OMAKASE_PLAYER_REVIEW_URL || 'http://127.0.0.1:8126');
    assert.equal(base.hostname, '127.0.0.1');
    const browser = await chromium.launch({...qaBrowser(), args: ['--mute-audio']});
    try {
      const page = await browser.newPage({viewport: {width: 1280, height: 900}});
      await page.addInitScript(() => {
        const play = HTMLMediaElement.prototype.play;
        HTMLMediaElement.prototype.play = function(...args) {
          this.muted = true;
          return play.apply(this, args);
        };
      });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const started = page.waitForResponse(response =>
        response.url().endsWith('/api/playback/catalog-sessions') && response.status() === 201
      );
      await page.goto(new URL('/review/sign-in?title=' + title, base).href);
      const session = await (await started).json();
      assert.equal(session.domain, title);
      await page.waitForFunction(() => {
        const media = document.querySelector('[data-player-video]');
        return !media.paused && media.currentTime > 0.2 && media.getVideoPlaybackQuality().totalVideoFrames > 2;
      });
      const timeline = page.locator('[data-player-timeline]');
      const box = await timeline.boundingBox();
      const saved = page.waitForResponse(async response =>
        response.url().endsWith('/events') && response.status() === 200 &&
        (await response.json()).position_seconds > 8
      );
      await timeline.click({position: {x: Math.round(box.width / 3), y: box.height / 2}});
      await saved;
      assert.ok(await page.locator('[data-player-error]').isHidden());

      const sidebar = page.locator('[data-player-sidebar-next]');
      if (title === 'tv') {
        assert.ok(await sidebar.isVisible());
        await sidebar.click();
        await page.getByRole('heading', {name: 'Episode 2', exact: true}).waitFor();
      } else {
        assert.ok(await sidebar.isHidden());
        const resumed = page.waitForResponse(response =>
          response.url().endsWith('/api/playback/catalog-sessions') && response.status() === 201
        );
        await page.reload();
        assert.ok((await (await resumed).json()).position_seconds > 8);
      }
      assert.deepEqual(errors, []);
    } finally {
      await browser.close();
    }
  });
}
