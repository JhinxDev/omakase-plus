/* Synthetic credential-loss failure must stop supplied, keyboard and native play. */
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {qaRuntime,qaBrowser,qaOutput}=require('../../scripts/qa-environment.cjs');
const {chromium}=require(path.join(qaRuntime(),'playwright'));
const base=process.env.OMAKASE_PLAYER_REVIEW_URL||'http://127.0.0.1:8136';
if(!['127.0.0.1','localhost'].includes(new URL(base).hostname))throw new Error('Synthetic loopback preview required');
const output=process.env.OMAKASE_PLAYER_QA_OUTPUT||qaOutput('player-failure');fs.mkdirSync(output,{recursive:true});
const root=path.resolve(__dirname,'../..');
const sources=['src/omakase/plus/static/web-player.js','src/omakase/plus/web_playback_routes.py','src/omakase/plus/web_playback_store.py'];
const identities=()=>Object.fromEntries(sources.map(file=>[file,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')]));
const assert=(ok,message)=>{if(!ok)throw new Error(message);};
(async()=>{
 const options=qaBrowser();const browser=await chromium.launch({...options,args:[...(options.args||[]),'--mute-audio']});
 const context=await browser.newContext({viewport:{width:1280,height:900},reducedMotion:'reduce'});
 await context.addInitScript(()=>{const play=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(...args){this.muted=true;return play.apply(this,args);};});
 const page=await context.newPage();const report={synthetic:true,sourceBefore:identities(),events:[],starts:0,errors:[],attempts:[]};
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('request',r=>{if(r.url().endsWith('/sessions')&&r.method()==='POST')report.starts++;});
 page.on('response',r=>{if(r.url().endsWith('/events'))report.events.push({status:r.status(),event:r.request().postDataJSON().event});});
 const probe=()=>page.locator('video').evaluate(video=>({position:video.currentTime,paused:video.paused,frames:video.getVideoPlaybackQuality().totalVideoFrames}));
 try{
  await page.goto(base+'/review/sign-in');await page.waitForFunction(()=>!document.querySelector('[data-player-start]')?.disabled);
  await page.getByRole('button',{name:'Play episode',exact:true}).click();await page.waitForFunction(()=>document.querySelector('video').currentTime>.4);
  await context.clearCookies({name:'omakase_session'});
  await page.locator('video').evaluate(video=>video.pause());
  await page.getByRole('button',{name:'Reload player',exact:true}).waitFor();
  assert(report.events.some(event=>event.status===401),'Actual credential-loss event must return401');
  report.failed=await probe();assert(report.failed.paused,'Terminal failure pauses playback');
  const eventCount=report.events.length;const starts=report.starts;
  async function attempt(name, action){await action();await page.waitForTimeout(300);const state=await probe();report.attempts.push({name,...state});assert(state.paused&&Math.abs(state.position-report.failed.position)<.05,name+' must not resume buffered playback');assert(report.events.length===eventCount&&report.starts===starts,name+' must not silently restart a stream or fabricate saves');}
  await page.locator('[data-player-container]').hover();
  await attempt('supplied Play',()=>page.getByRole('button',{name:'Play',exact:true}).first().click());
  await attempt('keyboard Play',async()=>{await page.getByRole('button',{name:'Play',exact:true}).first().focus();await page.keyboard.press('Space');});
  await attempt('native prototype Play',()=>page.locator('video').evaluate(video=>HTMLMediaElement.prototype.play.call(video).catch(error=>error.name)));
  report.feedback=await page.locator('[data-player-save]').textContent();
  assert(report.feedback==='Progress save interrupted.','Save feedback remains truthful after attempted buffered replay');
  await page.screenshot({path:path.join(output,'stopped-after-replay.png'),fullPage:true});
  // This explicit fixture sign-in plus manual Play is the qualified recovery;
  // no cookie or CSRF value is read, logged, copied or restored by the test.
  await page.goto(base+'/review/sign-in');await page.waitForFunction(()=>!document.querySelector('[data-player-start]')?.disabled);
  await page.getByRole('button',{name:'Play episode',exact:true}).click();await page.waitForFunction(()=>document.querySelector('video').currentTime>.3&&!document.querySelector('video').paused);
  report.recovered=await probe();assert(report.starts===starts+1,'Recovery requires exactly one new explicit Start');assert(report.errors.length===0,'No page errors');report.passed=true;
 }catch(error){report.passed=false;report.failure=error.message;await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});}
 finally{report.sourceAfter=identities();report.sourceStable=JSON.stringify(report.sourceBefore)===JSON.stringify(report.sourceAfter);report.passed=report.passed&&report.sourceStable;fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,failure:report.failure,failed:report.failed,attempts:report.attempts,recovered:report.recovered,errors:report.errors,sourceStable:report.sourceStable,output}));await context.close();await browser.close();}
})();
