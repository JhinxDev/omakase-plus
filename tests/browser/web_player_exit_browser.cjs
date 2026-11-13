/* Focused synthetic native seek/exit/re-entry regression. No media-preparation job. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {qaRuntime, qaBrowser, qaOutput} = require('../../scripts/qa-environment.cjs');
const {chromium} = require(path.join(qaRuntime(), 'playwright'));
const base = process.env.OMAKASE_PLAYER_REVIEW_URL || 'http://127.0.0.1:8137';
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) throw new Error('A synthetic loopback player preview is required');
const output = process.env.OMAKASE_PLAYER_QA_OUTPUT || qaOutput('player-exit');
fs.mkdirSync(output, {recursive:true});
const root = path.resolve(__dirname, '../..');
const sources = ['src/omakase/plus/static/web-player.js', 'src/omakase/plus/web_playback_routes.py', 'src/omakase/plus/web_playback_store.py'];
const identities = () => Object.fromEntries(sources.map(file => [file, crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')]));
const assert = (ok, message) => { if (!ok) throw new Error(message); };
(async () => {
  const options = qaBrowser();
  const browser = await chromium.launch({...options,args:[...(options.args||[]),'--mute-audio']});
  const context = await browser.newContext({viewport:{width:1280,height:900},reducedMotion:'reduce'});
  await context.addInitScript(() => {
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function(...args) {this.muted=true;return play.apply(this,args);};
    const fetch = window.fetch;
    window.fetch = function(input, options) {
      try {
        const pathname = new URL(typeof input==='string'?input:input.url,location.href).pathname;
        const sent = options?.body ? JSON.parse(options.body) : null;
        if (options?.method==='POST'&&pathname.endsWith('/close')&&sent?.final_event) {
          // Test-only bounded telemetry survives document unload. DevTools can
          // omit the old document's keepalive request/response events entirely.
          const key='omakase.qa.terminalAttempts';
          const attempts=JSON.parse(sessionStorage.getItem(key)||'[]');
          attempts.push({calledAt:Date.now(),keepalive:options.keepalive===true,seq:sent.final_event.seq,route:'/plus/api/playback/sessions/<session>/close'});
          sessionStorage.setItem(key,JSON.stringify(attempts.slice(-4)));
        }
      } catch { /* Observation must not alter the native request. */ }
      return fetch.apply(this,arguments);
    };
  });
  const page = await context.newPage();
  const reads = [];
  const report = {synthetic:true,mediaPreparationJobs:0,browser:browser.version(),sourceBefore:identities(),events:[],closeRequests:[],closes:[],starts:[],errors:[]};
  const evidence = async () => (await page.request.get(base+'/review/evidence')).json();
  page.on('pageerror', error => report.errors.push(error.message));
  context.on('request', request => {
    if (request.url().endsWith('/sessions') && request.method()==='POST') report.starts.push({requestedAt:Date.now()});
    if (request.url().endsWith('/close') && request.method()==='POST') report.closeRequests.push({requestedAt:Date.now(),sid:new URL(request.url()).pathname.split('/').at(-2),sent:request.postDataJSON()});
  });
  page.on('response', response => {
    if (response.url().endsWith('/events')) reads.push(response.json().then(accepted => report.events.push({at:Date.now(),acceptedAt:accepted.accepted_at,status:response.status(),sent:response.request().postDataJSON(),position:accepted.position_seconds,covered:accepted.covered_seconds})).catch(()=>{}));
    if (response.url().endsWith('/close')) report.closes.push({at:Date.now(),status:response.status(),sent:response.request().postDataJSON()});
  });
  try {
    await page.goto(base+'/review/sign-in');
    await page.waitForFunction(()=>!document.querySelector('[data-player-start]')?.disabled);
    await page.getByRole('button',{name:'Play episode',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('video').currentTime>.3);
    await page.locator('video').evaluate(video=>video.pause());
    for (let n=0;n<80&&!report.events.some(event=>event.sent.event==='pause'&&event.sent.position_seconds>.2);n++) await page.waitForTimeout(25);
    assert(report.events.some(event=>event.sent.event==='pause'&&event.sent.position_seconds>.2),'Initial played pause must acknowledge');
    report.beforeBackward = await evidence();
    await page.locator('video').evaluate(video=>{video.currentTime=27;});
    for (let n=0;n<80&&!report.events.some(event=>event.sent.event==='seek'&&event.position===27);n++) await page.waitForTimeout(25);
    const acknowledged27 = report.events.find(event=>event.sent.event==='seek'&&event.position===27);
    assert(acknowledged27,'Establish the real prior27second save');
    await page.locator('video').evaluate(video=>{video.currentTime=3;});
    await page.waitForFunction(()=>document.querySelector('video').currentTime===3&&!document.querySelector('video').seeking);
    report.beforeExit = await page.evaluate(()=>({position:document.querySelector('video').currentTime,save:document.querySelector('[data-player-save]').textContent}));
    report.exitAt = Date.now();
    assert(report.beforeExit.save!=='Progress saved.','An old27second ACK cannot claim that the newer3second seek is saved');
    // No sleep or acknowledgement wait is allowed between the backward seek,
    // ordinary reload, same-tab re-entry and the next explicit Play. Reload is
    // deliberately faster than a landing-page/sign-in detour: the second exact
    // close must reach the server while the first writer is still pacing.
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>!document.querySelector('[data-player-start]')?.disabled,null,{polling:10});
    report.checkpoint = await page.evaluate(()=>JSON.parse(sessionStorage.getItem('omakase.pendingTerminal.v1')||'null'));
    const nextStart = page.waitForResponse(response=>response.url().endsWith('/sessions')&&response.request().method()==='POST');
    await page.getByRole('button',{name:'Play episode',exact:true}).click();
    const response = await nextStart;
    const reopened = await response.json();
    report.reopened = {status:response.status(),position:reopened.position_seconds};
    assert(response.status()===201&&reopened.position_seconds===3,'Actual store must resume the final backward3second position');
    await page.waitForFunction(()=>document.querySelector('video').videoWidth>0&&document.querySelector('video').currentTime>=2.99&&document.querySelector('[data-player-start-panel]').hidden);
    await page.locator('video').evaluate(video=>video.pause());
    report.media = await page.locator('video').evaluate(video=>({position:video.currentTime,frames:video.getVideoPlaybackQuality().totalVideoFrames}));
    assert(Math.abs(report.media.position-3)<.15&&report.media.frames>0,'Native decoded playback must actually resume at3seconds');
    report.afterReopen = await evidence();
    report.remainingCheckpoint = await page.evaluate(()=>sessionStorage.getItem('omakase.pendingTerminal.v1'));
    assert(report.remainingCheckpoint===null,'Exact terminal acknowledgement clears the tab checkpoint');
    assert(report.closes.some(close=>close.status===204&&close.sent.final_event?.position_seconds===3),'Terminal save returns204');
    const replayRequests = report.closeRequests.filter(close=>close.sid===report.checkpoint?.session_id&&JSON.stringify(close.sent.final_event)===JSON.stringify(report.checkpoint?.final_event));
    report.fetchAttempts=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('omakase.qa.terminalAttempts')||'[]'));
    const matchingAttempts=report.fetchAttempts.filter(close=>close.seq===report.checkpoint?.final_event.seq);
    const keepalive=matchingAttempts.find(attempt=>attempt.keepalive);
    const replay=matchingAttempts.find(attempt=>!attempt.keepalive);
    report.replay = {observedRequests:replayRequests.length,attempts:matchingAttempts.length,predecessorAcceptedAt:acknowledged27.acceptedAt,
      firstCalledAt:keepalive?.calledAt,secondCalledAt:replay?.calledAt,earliestStrictWriteAt:acknowledged27.acceptedAt*1000+250,
      statuses:report.closes.filter(close=>close.sent.final_event?.seq===report.checkpoint?.final_event.seq).map(close=>close.status)};
    assert(keepalive&&replay&&replayRequests.length>=1,'Exit keepalive and immediate explicit re-entry submit the exact checkpoint');
    assert(report.replay.firstCalledAt>=acknowledged27.acceptedAt*1000&&report.replay.firstCalledAt<report.replay.earliestStrictWriteAt,'First terminal fetch is inside the predecessor’s250ms pacing window');
    assert(report.replay.secondCalledAt>=report.replay.firstCalledAt,'Checkpoint replay follows the exit attempt');
    assert(report.replay.secondCalledAt<report.replay.earliestStrictWriteAt,'Replay must overlap the predecessor’s250ms server pacing window');
    assert(report.replay.statuses.length>0&&report.replay.statuses.every(status=>status===204),'Overlapping exact terminal closes join with204, never429');
    const before = report.beforeBackward.coverage.reduce((sum,row)=>sum+row.covered_seconds,0);
    const after = report.afterReopen.coverage.reduce((sum,row)=>sum+row.covered_seconds,0);
    assert(after-before<.05&&report.afterReopen.completed===0&&report.afterReopen.watch_events===0,'Exit/backward jump cannot create played coverage or completion');
    assert(report.errors.length===0,'No page errors');
    await page.screenshot({path:path.join(output,'reopened.png'),fullPage:true});
    report.passed=true;
  } catch (error) {report.passed=false;report.failure=error.message;await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});}
  finally {
    await Promise.all(reads);
    report.sourceAfter=identities();
    report.sourceStable=JSON.stringify(report.sourceBefore)===JSON.stringify(report.sourceAfter);
    report.passed=report.passed&&report.sourceStable;
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({passed:report.passed,failure:report.failure,beforeExit:report.beforeExit,reopened:report.reopened,media:report.media,replay:report.replay,sourceStable:report.sourceStable,errors:report.errors,output}));
    await context.close();await browser.close();
  }
})();
