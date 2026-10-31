/* Focused synthetic supplied-control regression. No full watch or provider work. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {qaRuntime, qaBrowser, qaOutput} = require('../../scripts/qa-environment.cjs');
const {chromium} = require(path.join(qaRuntime(), 'playwright'));
const base = process.env.OMAKASE_PLAYER_REVIEW_URL || 'http://127.0.0.1:8136';
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) throw new Error('Use a synthetic loopback player preview');
const output = process.env.OMAKASE_PLAYER_QA_OUTPUT || process.env.QA_OUTPUT || qaOutput('player-controls');
fs.mkdirSync(output, {recursive:true});
const root = path.resolve(__dirname, '../..');
const files = ['web-player.js', 'web-player.css'];
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const identities = () => Object.fromEntries(files.map(file => [file, digest(fs.readFileSync(path.join(root,'src/omakase/plus/static',file)))]));
const assert = (value, message) => {if (!value) throw new Error(message);};
const closeEnough = (actual, expected, tolerance = .05) => Math.abs(actual-expected) <= tolerance;
(async () => {
  const options = qaBrowser();
  const browser = await chromium.launch({...options,args:[...(options.args||[]),'--mute-audio']});
  const report = {synthetic:true,browser:browser.version(),subjectiveAudio:false,physicalDevice:false,mediaPreparationJobs:0,
    sourceBefore:identities(),servedAssets:[],errors:[],desktop:{events:[],steps:[]},touch:{events:[]}};
  const reads = [];
  async function pageFor(kind, settings) {
    const context = await browser.newContext({...settings,reducedMotion:'reduce'});
    await context.addInitScript(() => {
      const nativePlay = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function(...args) {this.muted=true;return nativePlay.apply(this,args);};
    });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.on('pageerror',error=>report.errors.push({kind,message:error.message}));
    page.on('response',response=>{
      const pathname = new URL(response.url()).pathname;
      if (pathname.endsWith('/events')) reads.push(response.json().then(saved=>{
        const sent = response.request().postDataJSON();
        report[kind].events.push({status:response.status(),event:sent.event,position:saved.position_seconds,covered:saved.covered_seconds});
      }).catch(()=>{}));
      for (const file of files) if (pathname==='/plus/static/'+file) reads.push(response.body().then(body=>report.servedAssets.push({kind,file,sha256:digest(body)})));
    });
    return {context,page};
  }
  async function enter(page, touch = false) {
    await page.goto(base+'/review/sign-in');
    await page.waitForFunction(()=>!document.querySelector('[data-player-start]')?.disabled);
    if (await page.getByLabel('Resume from my saved position').isChecked()) await page.getByLabel('Resume from my saved position').uncheck();
    const start = page.getByRole('button',{name:'Play episode',exact:true});
    if (touch) await start.tap(); else await start.click();
    await page.waitForFunction(()=>{const v=document.querySelector('video');return v.currentTime>.3&&!v.paused&&v.videoWidth>0;});
  }
  async function pauseAck(page, state, touch = false) {
    if (touch) {
      await page.locator('[data-player-container]').tap();
      if (!await page.locator('video').evaluate(video=>video.paused)) await page.getByRole('button',{name:'Pause',exact:true}).first().tap();
    } else await page.locator('video').evaluate(video=>video.pause());
    const position = await page.locator('video').evaluate(video=>video.currentTime);
    for (let count=0;count<100;count++) {
      if (state.events.some(event=>event.status===200&&event.event==='pause'&&closeEnough(event.position,position))) return position;
      await page.waitForTimeout(25);
    }
    throw new Error('Actual pause acknowledgement missing');
  }
  let active = null;
  try {
    active = await pageFor('desktop',{viewport:{width:1280,height:900}});
    let {page} = active;
    await enter(page);
    const position = await pauseAck(page,report.desktop);
    await page.locator('[data-player-container]').hover();
    await page.locator('.shaka-overflow-menu-button').click();
    await page.locator('.shaka-playbackrate-button').click();
    const speed = page.getByRole('slider',{name:'Playback speed',exact:true});
    await speed.focus();
    for (const [key,expected] of [['End',2],['Home',.25],['ArrowRight',.3],['ArrowLeft',.25]]) {
      await speed.press(key);
      await page.waitForFunction(value=>Math.abs(document.querySelector('video').playbackRate-value)<.001,expected);
      const state = await page.locator('video').evaluate(video=>({position:video.currentTime,rate:video.playbackRate}));
      report.desktop.steps.push({control:'speed',key,...state});
      assert(closeEnough(state.position,position),'Speed keys must not seek the video');
    }
    report.desktop.ranges = await page.locator('input.shaka-range-element').evaluateAll(inputs=>inputs.map(input=>({
      name:input.getAttribute('aria-label'),height:input.getBoundingClientRect().height,
      padding:getComputedStyle(input).padding,border:getComputedStyle(input).borderWidth,min:input.min,max:input.max,
    })));
    assert(report.desktop.ranges.every(range=>range.name&&closeEnough(range.height,44,.1)&&range.padding==='0px'&&range.border==='0px'),'Supplied ranges retain names and44px hitboxes without app form padding');
    assert(await speed.getAttribute('min')==='0.25'&&await speed.getAttribute('max')==='2','Allowed speed range remains0.25to2');
    report.desktop.overlap = await page.evaluate(()=>document.querySelector('.shaka-playback-rate-slider').getBoundingClientRect().bottom>document.querySelector('.shaka-playback-rate-presets').getBoundingClientRect().top);
    assert(!report.desktop.overlap,'Speed slider must not cover preset buttons');
    await page.screenshot({path:path.join(output,'speed-desktop.png'),fullPage:true});
    await page.keyboard.press('Escape');await page.keyboard.press('Escape');
    report.desktop.focus = await page.evaluate(()=>document.activeElement?.getAttribute('aria-label'));
    assert(report.desktop.focus==='More settings','Escape twice restores More settings focus');
    // Process audio is muted; expose the independent volume value for native keys.
    await page.locator('video').evaluate(video=>{video.muted=false;video.volume=.5;});
    const volume = page.locator('input.shaka-volume-bar');await volume.focus();
    for (const [key,expected] of [['Home',0],['End',1]]) {
      await volume.press(key);
      const state = await page.locator('video').evaluate(video=>({position:video.currentTime,volume:video.volume}));
      report.desktop.steps.push({control:'volume',key,...state});
      assert(closeEnough(state.position,position)&&closeEnough(state.volume,expected,.001),'Volume navigation changes only its own value');
    }
    const seek = page.getByRole('slider',{name:'Seek',exact:true});
    await seek.focus();await seek.press('End');
    await page.waitForFunction(()=>{const v=document.querySelector('video');return Math.abs(v.currentTime-v.duration)<.1;});
    const beforeHome = report.desktop.events.length;
    await seek.press('Home');await page.waitForFunction(()=>document.querySelector('video').currentTime<.1);
    for (let n=0;n<100&&!report.desktop.events.slice(beforeHome).some(event=>event.event==='seek'&&event.position<.1);n++) await page.waitForTimeout(25);
    assert(report.desktop.events.slice(beforeHome).some(event=>event.event==='seek'&&event.position<.1),'Control-only fixture cleanup waits for its actual Home acknowledgement');
    report.desktop.seekHomeEnd = true;
    await active.context.close();active = null;

    active = await pageFor('touch',{viewport:{width:390,height:844},hasTouch:true,isMobile:true});
    page = active.page;
    await enter(page,true);await pauseAck(page,report.touch,true);
    const before = await (await page.request.get(base+'/review/evidence')).json();
    const touchSeek = page.getByRole('slider',{name:'Seek',exact:true});
    const box = await touchSeek.boundingBox();
    await touchSeek.tap({position:{x:box.width/2,y:box.height/2}});
    await page.waitForFunction(()=>Math.abs(document.querySelector('video').currentTime-15)<.1&&document.querySelector('video').paused);
    for (let n=0;n<100&&!report.touch.events.some(event=>event.event==='seek'&&closeEnough(event.position,15,.1));n++) await page.waitForTimeout(25);
    assert(report.touch.events.some(event=>event.event==='seek'&&closeEnough(event.position,15,.1)),'Normal touch Seek must reach its stored midpoint');
    const after = await (await page.request.get(base+'/review/evidence')).json();
    report.touch.seek = await page.locator('video').evaluate(video=>({position:video.currentTime,paused:video.paused}));
    report.touch.coverageDelta = after.coverage.reduce((sum,row)=>sum+row.covered_seconds,0)-before.coverage.reduce((sum,row)=>sum+row.covered_seconds,0);
    assert(report.touch.coverageDelta<.05&&after.completed===0&&after.watch_events===0,'Paused touch seeking cannot create watch coverage/completion');
    report.touch.layout = await page.locator('[data-player-container]').evaluate(stage=>({viewport:innerWidth,scrollWidth:document.documentElement.scrollWidth,stage:stage.getBoundingClientRect().width}));
    assert(report.touch.layout.scrollWidth===390&&report.touch.layout.stage<=390,'Loaded touch player fits390px');
    await page.screenshot({path:path.join(output,'touch-paused.png'),fullPage:true});
    await page.getByRole('button',{name:'Full screen',exact:true}).tap();
    await page.waitForFunction(()=>document.fullscreenElement?.matches('[data-player-container]'));
    report.touch.fullscreenEntered = true;
    await page.getByRole('button',{name:'Exit full screen',exact:true}).tap();
    await page.waitForFunction(()=>document.fullscreenElement===null);
    report.touch.fullscreenExited = true;
    report.touch.escapeScope = 'Emulated Escape is not a physical-device fullscreen acceptance check.';
    await Promise.all(reads);
    assert(report.servedAssets.length>=4&&report.servedAssets.every(asset=>asset.sha256===report.sourceBefore[asset.file]),'Served player assets match the tested source');
    assert(report.errors.length===0,'No page errors');
    assert([...report.desktop.events,...report.touch.events].every(event=>event.status===200),'All observed control saves accepted');
    report.passed = true;
  } catch (error) {
    report.passed=false;report.failure=error.message;
    if(active)await active.page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});
  } finally {
    if(active)await active.context.close();
    await browser.close();
    report.sourceAfter=identities();report.sourceStable=JSON.stringify(report.sourceBefore)===JSON.stringify(report.sourceAfter);
    report.passed=report.passed&&report.sourceStable;
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({passed:report.passed,failure:report.failure,sourceStable:report.sourceStable,desktopSteps:report.desktop.steps,touch:report.touch,errors:report.errors,output}));
    if(!report.passed)process.exitCode=1;
  }
})();
