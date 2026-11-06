/* One cached-only, muted sustained decode observation. No source/worker retries. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {qaRuntime, qaBrowser, qaOutput} = require('../../scripts/qa-environment.cjs');
const {chromium} = require(path.join(qaRuntime(), 'playwright'));
const base = process.env.OMAKASE_PLAYER_REVIEW_URL;
if (!base || !['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) {
  throw new Error('Set OMAKASE_PLAYER_REVIEW_URL to an operator-prepared cached-only loopback preview with worker launch disabled.');
}
const output = process.env.OMAKASE_PLAYER_QA_OUTPUT || process.env.QA_OUTPUT || qaOutput('player-stability');
fs.mkdirSync(output, {recursive:true});
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const css = path.resolve(__dirname, '../../src/omakase/plus/static/web-player.css');
const assert = (ok, code) => {if (!ok) throw new Error(code);};
const counters = evidence => Object.fromEntries(Object.entries(evidence.qualification || {}).filter(([key,value]) => /(?:jobs|launch|running|requests|reads)/.test(key) && (typeof value==='number' || typeof value==='boolean')));
(async () => {
  const options = qaBrowser();
  const browser = await chromium.launch({...options,args:[...(options.args || []),'--mute-audio']});
  const report = {schema:'player-stability-v1',browser:browser.version(),cachedOnlyOperatorPrerequisite:true,
    scope:'One explicit Start; canplay, 3s settling, 12s measured wall playback at1x. Private muted viewing; no subjective audio or physical-device smoothness claim.',
    cssSourceSha256:hash(fs.readFileSync(css)),samples:[],pageErrors:[],shakaErrors:[],httpFailures:[],mediaEvents:[]};
  const reads = [];
  const context = await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  const page = await context.newPage();
  try {
    await context.addInitScript(() => {
      const play = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function(...args) {this.muted=true;this.volume=0;return play.apply(this,args);};
    });
    page.on('pageerror', error => report.pageErrors.push({type:error.name,messageSha256:hash(error.message)}));
    page.on('response', response => {
      const route = new URL(response.url()).pathname;
      if (route==='/plus/static/web-player.css') reads.push(response.body().then(body => {report.cssServedSha256=hash(body);}));
      if (response.status()>=400) report.httpFailures.push({status:response.status(),kind:route.endsWith('/events')?'event':route.includes('/resources/')?'resource':'other'});
    });
    await page.goto(base+'/review/sign-in');
    await page.waitForFunction(() => window.shaka && !document.querySelector('[data-player-start]')?.disabled);
    const before = await (await page.request.get(base+'/review/evidence')).json();
    report.before = counters(before);
    await page.evaluate(() => {
      const attach = shaka.Player.prototype.attach;
      shaka.Player.prototype.attach = function(...args) {
        window.__stabilityPlayer=this;
        this.addEventListener('error', event => {
          const {category,code,severity}=event.detail;
          window.__stabilityShakaErrors.push({category,code,severity});
        });
        return attach.apply(this,args);
      };
      window.__stabilityShakaErrors=[];
      window.__stabilityEvents=[];
      const video=document.querySelector('video');
      for (const type of ['canplay','playing','waiting','stalled','error','pause','ended']) video.addEventListener(type,()=>window.__stabilityEvents.push({type,wallMs:performance.now(),position:video.currentTime,readyState:video.readyState}));
    });
    const resume = page.getByLabel('Resume from my saved position');
    if (await resume.isChecked()) await resume.uncheck();
    await page.getByRole('button',{name:'Play episode',exact:true}).click();
    await page.waitForFunction(() => {
      const video=document.querySelector('video');
      return video.readyState>=3 && video.currentTime>.1 && !video.paused && video.videoWidth>0;
    }, null, {timeout:30000});
    await page.waitForTimeout(3000);
    report.measurement = await page.evaluate(async () => {
      const video=document.querySelector('video'),player=window.__stabilityPlayer;
      const start=performance.now(),samples=[],callbacks=[];
      const snapshot=()=>{
        const quality=video.getVideoPlaybackQuality();
        const buffered=Array.from({length:video.buffered.length},(_,index)=>[video.buffered.start(index),video.buffered.end(index)]);
        const range=buffered.find(([from,to])=>from<=video.currentTime && to>=video.currentTime);
        return {wallMs:performance.now()-start,position:video.currentTime,decodedFrames:quality.totalVideoFrames,droppedFrames:quality.droppedVideoFrames,
          buffered,bufferAheadSeconds:range?range[1]-video.currentTime:0,readyState:video.readyState,networkState:video.networkState,
          paused:video.paused,muted:video.muted,volume:video.volume,rate:video.playbackRate,errorCode:video.error?.code||null};
      };
      let callbackId;
      const frame=(now,metadata)=>{callbacks.push({wallMs:now-start,mediaTime:metadata.mediaTime,presentedFrames:metadata.presentedFrames});callbackId=video.requestVideoFrameCallback(frame);};
      if (video.requestVideoFrameCallback) callbackId=video.requestVideoFrameCallback(frame);
      samples.push(snapshot());
      for (let index=0;index<12;index++) {await new Promise(resolve=>setTimeout(resolve,1000));samples.push(snapshot());}
      if (callbackId!==undefined) video.cancelVideoFrameCallback(callbackId);
      const variants=player.getVariantTracks().filter(track=>track.active).map(track=>({videoCodec:track.videoCodec,audioCodec:track.audioCodec,width:track.width,height:track.height,frameRate:track.frameRate,bandwidth:track.bandwidth}));
      const stats=player.getStats();
      return {startPerformanceMs:start,samples,callbacks,activeVariants:variants,dimensions:{width:video.videoWidth,height:video.videoHeight},
        shaka:{decodedFrames:stats.decodedFrames,droppedFrames:stats.droppedFrames,estimatedBandwidth:stats.estimatedBandwidth,bufferingTime:stats.bufferingTime,playTime:stats.playTime}};
    });
    await page.locator('video').evaluate(video=>video.pause());
    report.mediaEvents=await page.evaluate(()=>window.__stabilityEvents);
    report.shakaErrors=await page.evaluate(()=>window.__stabilityShakaErrors);
    const measurement=report.measurement,first=measurement.samples[0],last=measurement.samples.at(-1);
    const seconds=(last.wallMs-first.wallMs)/1000;
    const decoded=last.decodedFrames-first.decodedFrames,dropped=last.droppedFrames-first.droppedFrames;
    const callbackGaps=measurement.callbacks.slice(1).map((frame,index)=>frame.wallMs-measurement.callbacks[index].wallMs);
    report.summary={wallSeconds:seconds,mediaSeconds:last.position-first.position,decodedFrames:decoded,droppedFrames:dropped,
      decodedFps:decoded/seconds,droppedFraction:decoded?dropped/decoded:null,videoFrameCallbacks:measurement.callbacks.length,
      maximumPresentationGapMs:callbackGaps.length?Math.max(...callbackGaps):null,
      minimumBufferAheadSeconds:Math.min(...measurement.samples.map(sample=>sample.bufferAheadSeconds)),
      measurementEvents:report.mediaEvents.filter(event=>event.wallMs>=measurement.startPerformanceMs && event.wallMs<=measurement.startPerformanceMs+last.wallMs),
      minimumOneSecondMediaAdvance:Math.min(...measurement.samples.slice(1).map((sample,index)=>sample.position-measurement.samples[index].position))};
    const after = await (await page.request.get(base+'/review/evidence')).json();
    report.after=counters(after);
    await Promise.all(reads);
    report.cssSourceAfterSha256=hash(fs.readFileSync(css));
    assert(report.cssServedSha256===report.cssSourceSha256 && report.cssSourceAfterSha256===report.cssSourceSha256,'css_identity_changed');
    assert(JSON.stringify(report.before)===JSON.stringify(report.after),'cached_job_or_provider_counters_changed');
    assert(measurement.samples.every(sample=>sample.muted && sample.volume===0 && sample.rate===1 && !sample.paused && !sample.errorCode),'native_playback_not_continuously_running_at_muted_1x');
    assert(decoded>0 && seconds>=12 && report.summary.mediaSeconds>=seconds-.5,'insufficient_sustained_media_advancement');
    assert(!report.summary.measurementEvents.some(event=>['waiting','stalled','error','pause','ended'].includes(event.type)),'measured_waiting_stalled_or_terminal_event');
    assert(!report.pageErrors.length && !report.shakaErrors.length && !report.httpFailures.length,'runtime_or_http_failure');
    report.passed=true;
  } catch (error) {
    report.passed=false;report.failure={type:error.name,code:/^[a-z_]+$/.test(error.message)?error.message:'harness_or_prerequisite_failure'};
  } finally {
    await context.close();await browser.close();
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({passed:report.passed,failure:report.failure,summary:report.summary,variants:report.measurement?.activeVariants,cssSha256:report.cssServedSha256,output}));
    if(!report.passed)process.exitCode=1;
  }
})();
