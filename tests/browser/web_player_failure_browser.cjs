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
const mode=process.argv[2]||'desktop';
const layoutOnly=mode==='desktop-layout';
const viewport=mode==='phone'?{width:390,height:844}:layoutOnly?{width:1440,height:900}:{width:1280,height:900};
const sources=['src/omakase/plus/static/web-player.js','src/omakase/plus/static/web-player.css','src/omakase/plus/templates/player.html','src/omakase/plus/web_playback_routes.py','src/omakase/plus/web_playback_store.py'];
const identities=()=>Object.fromEntries(sources.map(file=>[file,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')]));
const assert=(ok,message)=>{if(!ok)throw new Error(message);};
const textContrast = node => {
 const luminance = value => value.match(/\d+/g).map(Number).reduce((sum, channel, i) => {
  const unit = channel / 255;
  return sum + [.2126,.7152,.0722][i] * (unit <= .04045 ? unit / 12.92 : ((unit + .055) / 1.055) ** 2.4);
 }, 0);
 const style = getComputedStyle(node);
 const levels = [luminance(style.backgroundColor),luminance(style.color)].sort((a,b)=>b-a);
 return (levels[0]+.05)/(levels[1]+.05);
};
(async()=>{
 const options=qaBrowser();const browser=await chromium.launch({...options,args:[...(options.args||[]),'--mute-audio']});
 const context=await browser.newContext({viewport,reducedMotion:'reduce',hasTouch:mode==='phone'});
 await context.addInitScript(()=>{const play=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(...args){this.muted=true;return play.apply(this,args);};});
 const page=await context.newPage();const report={synthetic:true,mode,sourceBefore:identities(),events:[],starts:0,errors:[],attempts:[]};
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('request',r=>{if(r.url().endsWith('/sessions')&&r.method()==='POST')report.starts++;});
 page.on('response',r=>{if(r.url().endsWith('/events'))report.events.push({status:r.status(),event:r.request().postDataJSON().event});});
 const probe=()=>page.locator('video').evaluate(video=>({position:video.currentTime,paused:video.paused,frames:video.getVideoPlaybackQuality().totalVideoFrames,readyState:video.readyState,buffered:video.buffered.length,srcAttribute:video.hasAttribute('src'),srcObject:!!video.srcObject,currentSrc:!!video.currentSrc}));
 try{
  await page.goto(base+'/review/sign-in');await page.waitForFunction(()=>!document.querySelector('[data-player-start]')?.disabled);
  await page.keyboard.press('Tab');
  const skip = page.getByRole('link', {name:'Skip to content'});
  const skipBox = await skip.boundingBox();
  const headerBox = await page.locator('.web-player-header').boundingBox();
  assert(await skip.evaluate(node=>node===document.activeElement),'First Tab reaches the skip link');
  assert(skipBox.y>=0&&skipBox.y+skipBox.height<=headerBox.y,'Focused skip link sits above the player title');
  report.skipContrast = await skip.evaluate(textContrast);
  assert(report.skipContrast>=7,'Skip link label meets enhanced text contrast');
  await page.screenshot({path:path.join(output,'focused-skip.png')});
  await page.keyboard.press('Enter');
  assert(await page.locator('#main-content').evaluate(node=>node===document.activeElement),'Skip link moves focus to main content');
  await page.getByRole('button',{name:'Play episode',exact:true}).click();await page.waitForFunction(()=>document.querySelector('video').currentTime>.4);
  if(mode==='fullscreen') {
    await page.locator('.shaka-fullscreen-button').click();
    await page.waitForFunction(()=>document.fullscreenElement?.matches('[data-player-container]'));
  }
  await page.getByRole('button',{name:'Pause',exact:true}).first().focus();
  await context.clearCookies({name:'omakase_session'});
  await page.locator('video').evaluate(video=>video.pause());
  const reload=page.getByRole('button',{name:'Reload player',exact:true});
  await reload.waitFor();
  const recovery=await reload.boundingBox();
  assert(recovery.y>=0&&recovery.y+recovery.height<=viewport.height,'Recovery action appears inside the viewport');
  assert(await reload.evaluate(node=>node===document.activeElement),'Focused transport hands off to Reload player');
  assert(await reload.evaluate(node=>node.classList.contains('web-player-primary')),'Reload uses the filled primary treatment');
  report.reloadContrast = await reload.evaluate(textContrast);
  assert(report.reloadContrast>=4.5,'Reload label meets text contrast');
  const back = page.getByRole('link',{name:'Back to title',exact:true});
  assert(await back.count()===1,'Recovery exposes one visible Back to title link');
  const backBox = await back.boundingBox();
  assert(backBox.y>=0&&backBox.y+backBox.height<=viewport.height&&backBox.height>=44,'Back is reachable in the viewport');
  assert(await page.evaluate(()=>document.fullscreenElement===null),'Recovery is reachable after fullscreen exits');
  if(mode==='phone') assert(!await page.locator('[data-player-container]').isVisible(),'Recovery replaces the empty video frame on phones');
  const transport = page.locator('.shaka-controls-container');
  assert(!await transport.isVisible()&&!await page.locator('.web-player-timeline').isVisible(),'Recovery hides transport and timeline');
  assert(report.events.some(event=>event.status===401),'Actual credential-loss event must return401');
  await page.waitForFunction(()=>{const video=document.querySelector('video');return video.paused&&video.readyState===0&&!video.hasAttribute('src')&&!video.srcObject&&!video.buffered.length;},null,{timeout:5000}).catch(async error=>{report.release=await probe();throw error;});
  report.failed=await probe();
  if(layoutOnly){
    report.context=await page.locator('.web-player-details').evaluate(node=>node.getBoundingClientRect().toJSON());
    report.heading=await page.locator('#episode-heading').evaluate(node=>node.getBoundingClientRect().toJSON());
    assert(report.heading.bottom<viewport.height&&report.context.top<viewport.height,'episode context remains above the desktop fold');
    assert(!await page.locator('[data-player-container]').isVisible(),'empty failed stage does not leave a full-height void');
    await page.screenshot({path:path.join(output,'compact-recovery.png')});
    assert(!report.errors.length,'no recovery layout page errors');report.passed=true;return;
  }
  const eventCount=report.events.length;const starts=report.starts;
  async function attempt(name, action){await action();await page.waitForTimeout(300);const state=await probe();report.attempts.push({name,...state});assert(state.readyState===0&&!state.buffered&&state.frames===report.failed.frames&&state.position===report.failed.position,name+' must not resume buffered playback');assert(report.events.length===eventCount&&report.starts===starts,name+' must not silently restart a stream or fabricate saves');}
  await page.screenshot({path:path.join(output,'focused-recovery.png')});
  await attempt('supplied Play',()=>transport.getByRole('button',{name:'Play',exact:true,includeHidden:true}).first().evaluate(button=>button.click()));
  await attempt('keyboard Play',async()=>{await page.keyboard.press('Tab');assert(await back.evaluate(node=>node===document.activeElement),'Tab moves from Reload to Back to title');await page.keyboard.press('Space');});
  await attempt('native prototype Play',()=>page.locator('video').evaluate(video=>{void HTMLMediaElement.prototype.play.call(video).catch(()=>{});}));
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
