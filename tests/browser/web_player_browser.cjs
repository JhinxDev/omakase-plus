/* Maintained synthetic browser-to-route player regression. Preview runs separately. */
const fs = require('node:fs');
const path = require('node:path');
const {qaRuntime, qaBrowser} = require('../../scripts/qa-environment.cjs');
const {chromium} = require(path.join(qaRuntime(), 'playwright'));
const base = process.env.OMAKASE_PLAYER_REVIEW_URL || 'http://127.0.0.1:8126';
const fullWatchOnly = process.env.OMAKASE_PLAYER_FULL_WATCH_ONLY === '1';
const output = process.env.OMAKASE_PLAYER_QA_OUTPUT || path.join(require('node:os').tmpdir(), 'omakase-player-browser');
fs.mkdirSync(output, {recursive:true});
const assert = (ok, message) => { if (!ok) throw new Error(message); };
(async () => {
  const browser = await chromium.launch(qaBrowser());
  const report = {url:base, synthetic:true, subjectiveAudio:false, browser:browser.version(), events:[], errors:[], requests:[]};
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}, reducedMotion:'reduce'});
    await context.addInitScript(() => {
      const original = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function (...args) { this.muted = true; return original.apply(this,args); };
    });
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    page.on('request', req => report.requests.push({url:req.url(),method:req.method()}));
    page.on('response', async res => {
      if (res.url().includes('/events')) {
        const event = res.request().postDataJSON();
        const accepted = await res.json().catch(() => ({}));
        report.events.push({status:res.status(),event:event.event,position:event.position_seconds,seq:event.seq,
          playing:event.playing,rate:event.playback_rate,accepted_at:accepted.accepted_at,
          covered_seconds:accepted.covered_seconds,credited_seconds:accepted.credited_seconds,
          watch_covered_seconds:accepted.watch_covered_seconds});
      }
    });
    await page.goto(base);
    await page.getByRole('link',{name:'Enter the synthetic review account'}).click();
    await page.getByRole('button',{name:'Play episode',exact:true}).waitFor();
    await page.waitForFunction(() => !document.querySelector('[data-player-start]').disabled);
    await page.screenshot({path:path.join(output,'desktop-ready.png'), fullPage:true});
    assert(await page.locator('video').evaluate(video => video.currentTime === 0 && video.paused), 'No autoplay before Play');
    await page.getByRole('button',{name:'Play episode',exact:true}).click();
    await page.waitForFunction(value => document.querySelector('video')?.currentTime > value, fullWatchOnly ? .3 : 1, {timeout:15000});
    report.initial = await page.locator('video').evaluate(video => ({time:video.currentTime,paused:video.paused,
      width:video.videoWidth,height:video.videoHeight,frames:video.getVideoPlaybackQuality().totalVideoFrames}));
    assert(report.initial.frames>0 && report.initial.width>0,'Decoded frames required');
    await page.locator('video').evaluate(video => video.pause());
    await page.waitForTimeout(fullWatchOnly ? 0 : 700);
    assert(!(await page.locator('[data-player-error]').isVisible()), 'Initial play/pause burst must save');
    if (!fullWatchOnly) {
    for (const position of [3,15,27,8]) {
      await page.locator('video').evaluate((video, value) => { video.currentTime=value; }, position);
      await page.waitForFunction(value => Math.abs(document.querySelector('video').currentTime-value)<.4, position);
      await page.waitForTimeout(700);
      assert(!(await page.locator('[data-player-error]').isVisible()), 'Seek burst must save');
    }
    await page.locator('video').evaluate(video => video.play());
    await page.waitForTimeout(2200);
    await page.locator('video').evaluate(video => video.pause());
    await page.waitForTimeout(1200);
    report.paused = await page.locator('video').evaluate(video => video.currentTime);
    await page.screenshot({path:path.join(output,'desktop-paused.png'),fullPage:true});
    await page.reload();
    await page.waitForFunction(() => !document.querySelector('[data-player-start]').disabled);
    await page.getByRole('button',{name:'Play episode',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('video')?.currentTime > 5, {timeout:15000});
    report.resumed = await page.locator('video').evaluate(video => video.currentTime);
    assert(Math.abs(report.resumed-report.paused)<2, 'Personal exact-version resume required');
    await page.locator('video').evaluate(video => video.pause());
    await page.waitForTimeout(800);
    }
    report.beforeFullWatch = await (await page.request.get(base+'/review/evidence')).json();
    assert(report.beforeFullWatch.completed===0,'Seek-only and small played intervals cannot complete episode');
    await page.locator('video').evaluate(video => {video.currentTime=0;video.playbackRate=2;});
    await page.waitForFunction(() => !document.querySelector('video').seeking);
    await page.waitForTimeout(fullWatchOnly ? 20 : 1000);
    await page.locator('video').evaluate(video => video.play());
    await page.waitForFunction(() => document.querySelector('video').ended, null, {timeout:25000});
    await page.waitForTimeout(1500);
    report.afterFullWatch = await (await page.request.get(base+'/review/evidence')).json();
    assert(report.afterFullWatch.completed===1 && report.afterFullWatch.watch_events===1,'Real full playback completes exactly once through watch service');
    assert(report.afterFullWatch.coverage[0].covered_seconds>=25.5,'At least 85 percent must be genuinely played');
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(output,'phone-paused.png'),fullPage:true});
    report.phone = await page.evaluate(() => ({width:innerWidth,scroll:document.documentElement.scrollWidth}));
    assert(report.phone.scroll <= report.phone.width+1, 'No phone horizontal overflow');
    assert(report.events.every(event=>event.status===200), 'All actual native-event requests must be accepted');
    assert(report.errors.length===0, 'No page errors');
    assert(report.requests.every(req=>new URL(req.url).origin===new URL(base).origin), 'All runtime assets/media same origin');
    report.passed=true;
    await context.close();
  } catch (error) {
    report.passed=false; report.failure=error.stack;
    throw error;
  } finally {
    fs.writeFileSync(path.join(output,'result.json'), JSON.stringify(report,null,2));
    await browser.close();
    console.log(JSON.stringify({passed:report.passed,failure:report.failure,initial:report.initial,paused:report.paused,resumed:report.resumed,phone:report.phone,eventCount:report.events.length,errors:report.errors,output}));
  }
})();
