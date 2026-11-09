/* Focused opaque video-overlay contrast on a loaded bright synthetic frame. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {qaRuntime,qaBrowser,qaOutput}=require('../../scripts/qa-environment.cjs');
const {chromium}=require(path.join(qaRuntime(),'playwright'));
const base=process.env.OMAKASE_PLAYER_REVIEW_URL||'http://127.0.0.1:8126';
if(!['127.0.0.1','localhost'].includes(new URL(base).hostname))throw new Error('Use the synthetic loopback player preview');
const output=process.env.OMAKASE_PLAYER_QA_OUTPUT||qaOutput('player-contrast');fs.mkdirSync(output,{recursive:true});
const root=path.resolve(__dirname,'../..');
const files=['web-player.css','shaka-controls-5.3.3.css'];
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const identities=()=>Object.fromEntries(files.map(file=>[file,hash(fs.readFileSync(path.join(root,'src/omakase/plus/static',file)))]));
const assert=(ok,message)=>{if(!ok)throw new Error(message);};
(async()=>{
 const options=qaBrowser(),browser=await chromium.launch({...options,args:[...(options.args||[]),'--mute-audio']});
 const context=await browser.newContext({viewport:{width:1280,height:1100},reducedMotion:'reduce'});
 await context.addInitScript(()=>{const play=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(...args){this.muted=true;this.volume=0;return play.apply(this,args);};});
 const report={synthetic:true,scope:'Actual paused bright video; computed opaque backing/foreground, seek colors and focus at1280/390/320. Not full accessibility/device/ASS acceptance.',sourceBefore:identities(),served:[],states:[],errors:[]};
 const reads=[];const page=await context.newPage();page.on('pageerror',e=>report.errors.push(e.message));
 page.on('response',response=>{for(const file of files)if(new URL(response.url()).pathname==='/plus/static/'+file)reads.push(response.body().then(bytes=>report.served.push({file,sha256:hash(bytes)})));});
 try{
  const adapted=fs.readFileSync(path.join(root,'src/omakase/plus/static/shaka-controls-5.3.3.css'),'utf8');
  assert(adapted.slice(0,1024).includes('MODIFIED by Omakase Plus'),'Prominent adapted vendor notice required');
  const vendor=JSON.parse(fs.readFileSync(path.join(root,'THIRD-PARTY/shaka-player-5.3.3.json'),'utf8'));
  assert(vendor.adaptations['src/omakase/plus/static/shaka-controls-5.3.3.css'].served_sha256===hash(Buffer.from(adapted)),'Adapted vendor manifest matches served CSS');
  await page.goto(base+'/review/sign-in');await page.waitForFunction(()=>!document.querySelector('[data-player-start]')?.disabled);
  await page.getByLabel('Resume from my saved position').uncheck();await page.getByRole('button',{name:'Play episode',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video').currentTime>.5&&document.querySelector('video').videoWidth>0);
  await page.locator('video').evaluate(video=>{video.pause();video.currentTime=.54;});await page.waitForFunction(()=>!document.querySelector('video').seeking);
  for(const viewport of [{width:1280,height:1100},{width:390,height:844},{width:320,height:844},{width:844,height:390,fullscreen:true}]){
   const {width,height,fullscreen=false}=viewport;
   await page.setViewportSize({width,height});
   if(fullscreen){await page.locator('.shaka-fullscreen-button').click();await page.waitForFunction(()=>!!document.fullscreenElement);}
   await page.locator('[data-player-container]').hover();
   await page.locator('.shaka-overflow-menu-button').focus();await page.keyboard.press('Tab');await page.keyboard.press('Shift+Tab');
   const state=await page.evaluate(()=>{
    const rgb=value=>value.startsWith('#')
      ? (value.slice(1).match(/.{2}/g)||[]).map(part=>parseInt(part,16))
      : (value.match(/[\d.]+/g)||[]).map(Number);
    const luminance=color=>color.slice(0,3).map(value=>value/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4).reduce((sum,value,index)=>sum+value*[.2126,.7152,.0722][index],0);
    const contrast=(a,b)=>{const x=luminance(a),y=luminance(b);return(Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
    const backing=document.querySelector('.shaka-bottom-controls'),back=getComputedStyle(backing),bg=rgb(back.backgroundColor);
    const controls=[...backing.querySelectorAll('button,.shaka-time-and-duration')].filter(node=>!node.classList.contains('shaka-spacer')&&node.getClientRects().length&&getComputedStyle(node).display!=='none').map(node=>{
      const style=getComputedStyle(node),rect=node.getBoundingClientRect();let opacity=1;for(let el=node;el;el=el.parentElement){opacity*=Number(getComputedStyle(el).opacity);if(el.matches('[data-player-container]'))break;}
      return{name:node.getAttribute('aria-label')||node.textContent.trim(),foreground:style.color,opacity,ratio:contrast(rgb(style.color),bg),rect:rect.toJSON()};
    });
    const seek=document.querySelector('[data-player-timeline]');
    const rules=[...document.styleSheets].flatMap(sheet=>{try{return[...sheet.cssRules];}catch{return[];}});
    const seekBackground=rgb(getComputedStyle(seek.closest('[data-player-container]')).backgroundColor);
    const accent=getComputedStyle(seek).getPropertyValue('--stream-accent').trim() || '#c3b1ff';
    const resolvedAccent=css=>css.replace(/var\(--stream-accent,[^)]+\)/g,accent);
    const trackRule=rules.find(rule=>rule.selectorText==='.web-player input[data-player-timeline]::-webkit-slider-runnable-track');
    const track=resolvedAccent(trackRule.style.background);
    const trackColors=track.match(/#[\da-f]{6}\b|rgba?\([^)]+\)/gi) || [];
    const focus=document.activeElement,focusStyle=getComputedStyle(focus);
    const thumbRule=rules.find(rule=>rule.selectorText==='.web-player input[data-player-timeline]::-webkit-slider-thumb');
    const thumbFill=resolvedAccent(thumbRule.style.background);
    const video=document.querySelector('video');
    const videoRect=video.getBoundingClientRect(),scale=Math.min(video.clientWidth/video.videoWidth,video.clientHeight/video.videoHeight);
    const image={left:videoRect.left+(video.clientWidth-video.videoWidth*scale)/2,top:videoRect.top+(video.clientHeight-video.videoHeight*scale)/2,width:video.videoWidth*scale,height:video.videoHeight*scale};image.bottom=image.top+image.height;
    return{width:innerWidth,fullscreen:!!document.fullscreenElement,scroll:document.documentElement.scrollWidth,backing:backing.getBoundingClientRect().toJSON(),video:videoRect.toJSON(),image,media:{position:video.currentTime,paused:video.paused,muted:video.muted,frames:video.getVideoPlaybackQuality().totalVideoFrames},background:back.backgroundColor,backingOpacity:Number(back.opacity),controls,
      seek:{track,colors:trackColors.map(color=>({color,ratio:contrast(rgb(color),seekBackground)})),thumb:{fill:thumbFill,border:thumbRule.style.borderColor,ratio:contrast(rgb(thumbFill),rgb(thumbRule.style.borderColor))}},
      focus:{name:focus.getAttribute('aria-label'),visible:focus.matches(':focus-visible'),outline:focusStyle.outlineColor,width:focusStyle.outlineWidth,ratio:contrast(rgb(focusStyle.outlineColor),bg)}};
   });
   report.states.push(state);
   assert(state.scroll<=width,'No horizontal control overflow');assert(state.background==='rgb(8, 13, 26)'&&state.backingOpacity===1,'Known opaque backing over video');
   for(const name of [...(width>360?['Unmute']:[]),fullscreen?'Exit full screen':'Full screen','More settings']){const control=state.controls.find(control=>control.name===name);assert(control&&control.opacity===1&&control.ratio>=3,name+' must retain3:1 contrast and no alpha blend with video');assert(control.rect.width>=44&&control.rect.height>=44,name+' retains touch target');}
   assert(state.controls.every(control=>control.ratio>=3),'Time/active controls retain contrast');
   assert(state.controls.every(control=>control.rect.left>=state.backing.left&&control.rect.right<=state.backing.right),'Every active control, including Play, stays inside the opaque strip');
   assert(state.image.bottom<=state.backing.top&&state.image.height>0,'Opaque controls remain below the actual contain-fit video image and its ASS coordinate rectangle');
   assert(state.fullscreen===fullscreen,'Requested container fullscreen state retained');
   assert(state.seek.colors.length&&state.seek.colors.every(color=>color.ratio>=3)&&state.seek.thumb?.ratio>=3,'Seek track/thumb have dependable contrast');
   assert(state.focus.visible&&parseFloat(state.focus.width)>=2&&state.focus.ratio>=3,'Visible keyboard focus remains contrasting');
   assert(state.media.paused&&state.media.muted&&Math.abs(state.media.position-.54)<.01,'Same muted paused bright frame');
   await page.screenshot({path:path.join(output,'bright-'+width+(fullscreen?'-fullscreen':'')+'.png')});
  }
  await Promise.all(reads);assert(report.served.length>=2&&report.served.every(file=>file.sha256===report.sourceBefore[file.file]),'Actual served CSS matches frozen source');assert(!report.errors.length,'No page errors');report.passed=true;
 }catch(error){report.passed=false;report.failure=error.message;await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});}
 finally{report.sourceAfter=identities();report.sourceStable=JSON.stringify(report.sourceBefore)===JSON.stringify(report.sourceAfter);report.passed=report.passed&&report.sourceStable;fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,failure:report.failure,minimum:Math.min(...report.states.flatMap(state=>state.controls.map(control=>control.ratio))),states:report.states.map(state=>({width:state.width,focus:state.focus,seek:state.seek})),errors:report.errors,sourceStable:report.sourceStable,output}));await context.close();await browser.close();if(!report.passed)process.exitCode=1;}
})();
