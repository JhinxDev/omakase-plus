/* Real Shaka, synthetic loopback media and DB. Forced white backdrop is a
   contrast stress test, not a claim about encoded-white media. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {qaRuntime,qaBrowser,qaOutput}=require('../../scripts/qa-environment.cjs');
const {chromium}=require(require.resolve('playwright',{paths:[qaRuntime()]}));
const base=process.env.OMAKASE_PLAYER_REVIEW_URL||'http://127.0.0.1:8147';
assert.equal(new URL(base).hostname,'127.0.0.1');
const out=process.env.OMAKASE_PLAYER_QA_OUTPUT||qaOutput('player-transport');fs.mkdirSync(out,{recursive:true});
const layoutOnly=process.argv.includes('--layout-only');
const menuFlow=process.argv.includes('--menu-flow');
const trackMenus=process.argv.includes('--track-menus')||menuFlow;
const phoneRefinement=process.argv.includes('--phone-refinement')||trackMenus;
const assFit=process.argv.includes('--ass-fit');
const root=path.resolve(__dirname,'../..'),files=['src/omakase/plus/templates/player.html','src/omakase/plus/static/web-player.js','src/omakase/plus/static/web-player.css'];
const hashes=()=>Object.fromEntries(files.map(file=>[file,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')]));
(async()=>{
 const browser=await chromium.launch({...qaBrowser(),args:['--mute-audio']});
 const report={sourceBefore:hashes(),browser:browser.version(),synthetic:true,forcedWhiteBackdrop:true,physicalDevice:false,states:[],errors:[]};let current;
 try {
  for(const scenario of [{width:trackMenus?1440:1280},{width:390,touch:true},{width:320,touch:true},{width:390,text200:true},...(trackMenus?[{width:1440,text200:true}]:[])].filter(scenario=>(!layoutOnly||scenario.width===390)&&(!(assFit||menuFlow)||scenario.width<=640))) {
   const context=await browser.newContext({viewport:{width:scenario.width,height:scenario.width<500?844:900},hasTouch:!!scenario.touch,isMobile:!!scenario.touch,reducedMotion:'reduce'});
   const page=await context.newPage();current=page;page.setDefaultTimeout(10000);page.on('pageerror',error=>report.errors.push(error.message));
   await page.addInitScript(()=>{const play=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(...args){this.muted=true;return play.apply(this,args);};});
   if(phoneRefinement)await page.route(/\/api\/playback\/sessions$/,async route=>{const response=await route.fetch();const body=await response.json();return route.fulfill({response,json:{...body,episode_name:'Fixture episode name',episode_summary:'A synthetic episode summary, distinct from the series synopsis.'}});});
   if(assFit)await page.route('**/review/geometry.ass',route=>route.fulfill({contentType:'text/plain',body:`[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Liberation Sans,44,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,5,20,20,30,1\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.00,0:00:30.00,Default,,0,0,0,,{\\pos(320,90)}Synthetic ASS fit\n`}));
   await page.goto(base+'/review/sign-in');await page.waitForFunction(()=>!document.querySelector('[data-player-start]').disabled);
   await page.getByLabel('Resume from my saved position').uncheck();await page.getByRole('button',{name:'Play episode',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('video').currentTime>.1&&!document.querySelector('video').paused);
   await page.locator('video').evaluate(video=>video.pause());
   if(scenario.text200)await page.addStyleTag({content:'html{font-size:200%}'});
   if(phoneRefinement){
    assert.equal(await page.locator('[data-player-episode-heading]').textContent(),'Episode 1 · Fixture episode name');
    assert.equal(await page.locator('[data-player-episode-synopsis]').textContent(),'A synthetic episode summary, distinct from the series synopsis.');
    assert.equal(await page.locator('.web-player-header h1').textContent(),'Synthetic playback study');
    const desktopLink=page.locator('.web-player-tv-test--desktop'),phoneLink=page.locator('.web-player-tv-test--phone');
    assert.equal(await desktopLink.isVisible(),scenario.width>640,'desktop keeps its existing Apple TV action');
    if(scenario.width<=640){assert.equal(await phoneLink.isVisible(),false);await page.locator('.web-player-tools > summary').click();assert(await phoneLink.isVisible(),'phone Apple TV linking remains inside Playback options');assert.equal(await phoneLink.getAttribute('action'),'/plus/native-tv-test');await page.locator('.web-player-tools > summary').click();await page.evaluate(()=>scrollTo(0,0));}
   }
   const audio=page.getByRole('button',{name:'Audio language',exact:true}),subtitles=page.getByRole('button',{name:'Subtitles',exact:true});
   const audioAvailable=await audio.isEnabled();
   if(audioAvailable){await audio.click();assert(await page.locator('.shaka-audio-languages:not(.shaka-hidden)').isVisible(),'Audio opens directly');
    await page.keyboard.press('Escape');assert(await audio.evaluate(node=>node===document.activeElement),'Audio Escape restores its direct trigger');}
   else assert.equal(await audio.getAttribute('title'),'Audio is unavailable for this stream','unexposed audio is explicit, never invented');
   const stageBeforeMenu=await page.locator('[data-player-container]').boundingBox();
   const metadataBeforeMenu=await page.locator('.web-player-details').boundingBox();
   const closedMetadataGap=metadataBeforeMenu.y-stageBeforeMenu.y-stageBeforeMenu.height;
   await subtitles.click();assert(await page.locator('.omakase-subtitles-menu:not(.shaka-hidden)').isVisible(),'Subtitles opens directly');
   const rows=await page.locator('[data-subtitle-choice]').evaluateAll(nodes=>nodes.map(node=>{const style=getComputedStyle(node),range=document.createRange();range.selectNodeContents(node.querySelector('span')||node);return {text:node.textContent,fontSize:parseFloat(style.fontSize),color:style.color,ink:range.getBoundingClientRect().toJSON()};}));
   assert(rows.some(row=>row.text==='Off')&&rows.every(row=>row.fontSize>=14),'real subtitle choices, including Off, remain readable');
   const menuBox=await page.locator('.omakase-subtitles-menu').boundingBox();
   assert(rows.every(row=>row.ink.left>=menuBox.x-.1&&row.ink.right<=menuBox.x+menuBox.width+.1),'subtitle labels remain within the visible menu');
   let menuPlacement;
   let menuFlowProof;
   async function assertFlowRestored(){
    await page.waitForFunction(()=>document.querySelector('[data-player-track-slot]').hidden);
    const metadata=await page.locator('.web-player-details').boundingBox(),stage=await page.locator('[data-player-container]').boundingBox();
    assert(Math.abs(metadata.y-stage.y-stage.height-closedMetadataGap)<.2,'closing restores the original metadata position relative to the player');
   }
   if(trackMenus){
    menuPlacement=await page.evaluate(()=>{
     const menu=document.querySelector('.omakase-subtitles-menu'),rect=menu.getBoundingClientRect(),stage=document.querySelector('[data-player-container]').getBoundingClientRect(),video=document.querySelector('video').getBoundingClientRect(),bar=document.querySelector('.shaka-bottom-controls').getBoundingClientRect();
     const overlap=Math.max(0,Math.min(rect.right,video.right)-Math.max(rect.left,video.left))*Math.max(0,Math.min(rect.bottom,video.bottom)-Math.max(rect.top,video.top));
     const hit=document.elementFromPoint(rect.left+rect.width/2,rect.top+Math.min(22,rect.height/2));
     return {menu:rect.toJSON(),stage:stage.toJSON(),video:video.toJSON(),bar:bar.toJSON(),unobstructed:1-overlap/(video.width*video.height),hitInside:!!hit&&menu.contains(hit),audioTooltip:getComputedStyle(document.querySelector('.omakase-audio-button'),'::after').display};
    });
    assert(Math.abs(menuPlacement.stage.height-stageBeforeMenu.height)<.1,'opening a track menu never grows the stage');
    assert(menuPlacement.hitInside,'the panel is painted/hit-testable outside the stage, not merely present in the DOM');
    assert.equal(menuPlacement.audioTooltip,'none','track menus suppress the confirmed clipped hover-tooltip fragment');
    if(scenario.width<=640){
     assert(menuPlacement.unobstructed>=.85,'phone menu leaves at least85% of actual video unobstructed');
     assert(menuPlacement.menu.top>=menuPlacement.stage.bottom+7.5,'phone track panel sits below the player');
     await page.evaluate(()=>scrollBy(0,24));
     await page.waitForFunction(()=>{const m=document.querySelector('.omakase-subtitles-menu').getBoundingClientRect(),s=document.querySelector('[data-player-container]').getBoundingClientRect();return Math.abs(m.top-s.bottom-8)<1;});
     menuPlacement.afterScroll=await page.evaluate(()=>({scrollY,menu:document.querySelector('.omakase-subtitles-menu').getBoundingClientRect().toJSON(),stage:document.querySelector('[data-player-container]').getBoundingClientRect().toJSON()}));
     const canLeave=await page.evaluate(()=>document.documentElement.scrollHeight-innerHeight>document.querySelector('[data-player-container]').getBoundingClientRect().bottom+scrollY+20);
     if(canLeave){
      const target=await page.evaluate(()=>{const target=document.querySelector('[data-player-container]').getBoundingClientRect().bottom+scrollY+20;scrollTo(0,target);return target;});
      await page.waitForFunction(()=>document.querySelector('.omakase-subtitles-menu').classList.contains('shaka-hidden'));
      const position=await page.evaluate(()=>({y:scrollY,max:Math.max(0,document.documentElement.scrollHeight-innerHeight)}));
      assert(position.y>=Math.min(target,position.max)-1,`scroll dismissal stays at the clamped document position: ${JSON.stringify({target,...position})}`);
      menuPlacement.dismissedScroll={target,...position};
      menuPlacement.dismissedWhenAnchorLeft=true;
     }
     await page.evaluate(()=>scrollTo(0,0));
     if(await page.locator('.omakase-subtitles-menu.shaka-hidden').count())await subtitles.click();
    }else assert(menuPlacement.bar.top-menuPlacement.menu.bottom>=7.5,'desktop menu clears the entire transport by8px');
   }
   if(menuFlow){
    const slot=page.locator('[data-player-track-slot]');await slot.waitFor({state:'visible'});
    const menu=await page.locator('.omakase-subtitles-menu').boundingBox(),metadata=await page.locator('.web-player-details').boundingBox();
    assert(metadata.y>=menu.y+menu.height+8,'open phone panel never overlaps episode metadata');
    assert(Math.abs((await slot.boundingBox()).height-menu.height-8)<.2,'the in-flow slot reserves exactly the rendered panel and its gap');
    const seek=page.locator('[data-player-timeline]');
    const range=await seek.evaluate(node=>{const box=node.getBoundingClientRect(),style=getComputedStyle(node);return {ready:!node.disabled,visible:style.visibility==='visible',hit:document.elementFromPoint(box.left+box.width/2,box.top+box.height/2)===node,height:box.height};});
    assert(range.ready&&range.visible&&range.hit&&range.height>=43.9,'ready seek bar remains visible and hit-testable with track panel open');
    menuFlowProof={metadataClearance:metadata.y-menu.y-menu.height,seek:range,restore:[]};
    if(scenario.width===390){
     const box=await seek.boundingBox(),position={x:box.width/2,y:box.height/2};
     if(scenario.touch)await seek.tap({position});else await seek.click({position});
     await page.waitForFunction(()=>{const video=document.querySelector('video');return video.paused&&!video.seeking&&video.readyState>=2&&Math.abs(video.currentTime-15)<.15&&document.querySelector('[data-player-loading]').hidden&&/^0:1[45] \//.test(document.querySelector('[data-player-clock]').textContent);});
     assert(await page.locator('.omakase-subtitles-menu:not(.shaka-hidden)').isVisible(),'seeking can complete while the phone menu stays open');
     menuFlowProof.seek.position=await page.locator('video').evaluate(video=>video.currentTime);
    }
   }
   await page.screenshot({path:path.join(out,`subtitles-${scenario.width}${scenario.text200?'-text200':''}.png`)});
   if(phoneRefinement){await page.locator('[data-subtitle-choice="off"]').click();assert.equal(await page.locator('[data-subtitle-choice="off"]').getAttribute('aria-checked'),'true','caption Off selection remains functional');}
   await page.keyboard.press('Escape');assert(await subtitles.evaluate(node=>node===document.activeElement),'Subtitles Escape restores its direct trigger');
   if(menuFlow){await assertFlowRestored();menuFlowProof.restore.push('Escape');}
   if(trackMenus){await subtitles.click();await page.locator('.omakase-subtitles-menu .shaka-back-to-overflow-button').click();assert(await subtitles.evaluate(node=>node===document.activeElement),'Close restores the same track trigger');}
   if(menuFlow){
    await assertFlowRestored();menuFlowProof.restore.push('Close');await subtitles.click();
    if(scenario.touch)await page.touchscreen.tap(3,5);else await page.mouse.click(3,5);
    assert(await page.locator('.omakase-subtitles-menu.shaka-hidden').count(),'outside tap closes through the existing track menu lifecycle');
    await assertFlowRestored();menuFlowProof.restore.push('outside');
   }
   if(!layoutOnly&&!phoneRefinement){
   await page.locator('video').evaluate(video=>{video.currentTime=12;});await page.waitForFunction(()=>!document.querySelector('video').seeking);
   const before=await(await page.request.get(base+'/review/evidence')).json();
   await page.getByRole('button',{name:'Seek forward 10 seconds',exact:true}).click();
   await page.waitForFunction(()=>Math.abs(document.querySelector('video').currentTime-22)<.15&&document.querySelector('video').paused);
   await page.getByRole('button',{name:'Seek backward 10 seconds',exact:true}).click();
   await page.waitForFunction(()=>Math.abs(document.querySelector('video').currentTime-12)<.15&&document.querySelector('video').paused);
   const after=await(await page.request.get(base+'/review/evidence')).json();
   assert.equal(after.completed,before.completed,'paused seek controls do not invent completion');
   }
   const white=await page.addStyleTag({content:'[data-player-container]{background:#fff!important}[data-player-video]{opacity:0!important}'});
   await page.getByRole('button',{name:'Seek backward 10 seconds',exact:true}).focus();
   const facts=await page.evaluate(()=>{
    const lum=color=>color.slice(0,3).map(x=>x/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((sum,x,i)=>sum+x*[.2126,.7152,.0722][i],0);
    const rgb=value=>(value.match(/[\d.]+/g)||[]).map(Number),contrast=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
    const bar=document.querySelector('.shaka-bottom-controls'),back=rgb(getComputedStyle(bar).backgroundColor);
    const controls=[...document.querySelectorAll('.shaka-controls-button-panel > button,.shaka-controls-button-panel > a')].filter(node=>node.getClientRects().length&&!node.classList.contains('shaka-hidden')&&getComputedStyle(node).visibility==='visible').map(node=>({name:node.getAttribute('aria-label'),text:node.textContent.trim(),box:node.getBoundingClientRect().toJSON(),ratio:contrast(rgb(getComputedStyle(node).color),back),opacity:getComputedStyle(node).opacity}));
    const labelInk=[...document.querySelectorAll('[data-next-label],.shaka-controls-button-panel .shaka-overflow-button-label')].filter(node=>node.getClientRects().length).map(node=>{const range=document.createRange();range.selectNodeContents(node);const name=node.querySelector('span:first-child');let wholeWordLines;if(name){const word=document.createRange();word.selectNodeContents(name);wholeWordLines=[...word.getClientRects()].filter(rect=>rect.width>0&&rect.height>0).length;}return {text:node.textContent,wholeWordLines,ink:range.getBoundingClientRect().toJSON(),button:node.closest('button,a').getBoundingClientRect().toJSON()};});
    const clock=document.querySelector('[data-player-clock]');
    const video=document.querySelector('video'),stage=document.querySelector('[data-player-container]');
    const secondaryBaseline=[...document.querySelectorAll('[data-next-label],.omakase-audio-button .shaka-overflow-button-label > span:first-child,.omakase-subtitles-button .shaka-overflow-button-label > span:first-child')].filter(node=>node.getClientRects().length).map(node=>{const range=document.createRange();range.selectNodeContents(node);return {text:node.textContent,bottom:range.getBoundingClientRect().bottom};});
    return {overflow:document.documentElement.scrollWidth-innerWidth,controls,labelInk,secondaryBaseline,video:video.getBoundingClientRect().toJSON(),intrinsicRatio:video.videoWidth/video.videoHeight,secondaryBand:parseFloat(getComputedStyle(stage).paddingBottom),timeContrast:contrast(rgb(getComputedStyle(clock).color),back),bar:bar.getBoundingClientRect().toJSON(),background:getComputedStyle(bar).backgroundColor,nextLabel:document.querySelector('[data-next-label]').getBoundingClientRect().toJSON(),scrollY,stage:stage.getBoundingClientRect().toJSON()};
   });
   report.states.push({...scenario,audioAvailable,subtitleRows:rows,menuPlacement,menuFlowProof,...facts});
   assert(facts.overflow<=1,'no horizontal page overflow');assert.equal(facts.background,'rgb(8, 13, 26)');
   if(phoneRefinement&&scenario.width<=640){
    assert(Math.abs(facts.video.width/facts.video.height-facts.intrinsicRatio)<.01,'phone video retains its actual intrinsic aspect without internal letterbox stretching');
    assert(facts.video.top-facts.stage.top<=1.1,'video begins at the frame edge without an empty top bar');
    assert(facts.stage.height-facts.video.height-facts.secondaryBand<=2.1,'extra stage height contains only the necessary secondary control band');
    if(scenario.width===390&&!scenario.text200){assert(facts.video.top<=100,'phone video begins within100px');const baseline=facts.secondaryBaseline.map(label=>label.bottom);assert(Math.max(...baseline)-Math.min(...baseline)<=1.1,'secondary labels share a baseline');}
   }
   assert(facts.timeContrast>=4.5,'timecode foreground remains legible over the white backdrop');
   for(const label of facts.labelInk)assert(label.ink.left>=label.button.left-.5&&label.ink.right<=label.button.right+.5,`${label.text} stays inside its own target at text zoom`);
   for(const label of facts.labelInk.filter(label=>label.wholeWordLines!==undefined))assert.equal(label.wholeWordLines,1,'Audio and Subtitles remain complete words on one line');
   for(const control of facts.controls){assert(control.box.width>=43.9&&control.box.height>=43.9,`${control.name} target ${JSON.stringify(control.box)}`);assert(control.ratio>=4.5,`${control.name} contrast ${control.ratio}`);assert(control.box.left>=facts.bar.left-.1&&control.box.right<=facts.bar.right+.1,`${control.name} inside scrim`);}
   for(let i=0;i<facts.controls.length;i++)for(let j=i+1;j<facts.controls.length;j++){const a=facts.controls[i].box,b=facts.controls[j].box;assert(Math.min(a.right,b.right)-Math.max(a.left,b.left)<1||Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)<1,'transport targets do not overlap');}
   assert(facts.nextLabel.width>0,'Next episode has visible text');
   for(const name of ['Seek backward 10 seconds','Seek forward 10 seconds','Audio language','Subtitles','Full screen'])assert(facts.controls.some(control=>control.name===name),`${name} is directly visible`);
   await page.evaluate(()=>scrollTo(0,0));
   await page.screenshot({path:path.join(out,`transport-white-${scenario.width}${scenario.text200?'-text200':''}.png`)});
   await white.evaluate(node=>node.remove());
   if(assFit){
    await page.evaluate(async()=>{const {OmakaseASS}=await import('/plus/static/web-ass.js');window.__assFitErrors=[];window.__assFit=new OmakaseASS({video:document.querySelector('video'),container:document.querySelector('[data-player-container]'),onError:error=>__assFitErrors.push(error.message)});await __assFit.setTrack({url:'/review/geometry.ass'});});
    await page.waitForTimeout(200);
    if(trackMenus)await subtitles.click();
    const fitting=await page.evaluate(()=>({video:document.querySelector('video').getBoundingClientRect().toJSON(),canvas:document.querySelector('.omakase-ass-overlay').getBoundingClientRect().toJSON(),errors:__assFitErrors}));
    for(const key of ['left','top','width','height'])assert(Math.abs(fitting.video[key]-fitting.canvas[key])<=1.1,`ASS canvas ${key} stays tied to the actual video rather than the secondary controls`);
    assert.deepEqual(fitting.errors,[]);report.states[report.states.length-1].assFit=fitting;
    await page.screenshot({path:path.join(out,`ass-fit-${scenario.width}${scenario.text200?'-text200':''}.png`)});
    if(trackMenus)await page.keyboard.press('Escape');
    await page.evaluate(async()=>{await __assFit.clear();await __assFit.destroy();});
    assert.equal(await page.locator('.omakase-ass-overlay').count(),0,'synthetic ASS Off clears the actual canvas');
   }
   if(scenario.width===1280&&!phoneRefinement){
    await page.locator('video').evaluate(video=>video.play());await page.evaluate(()=>document.activeElement.blur());await page.mouse.move(500,300);
    await page.waitForFunction(()=>document.querySelector('[data-player-container]').hasAttribute('data-cinema-idle'));
    await page.screenshot({path:path.join(out,'cinema-idle-1280.png')});
    await page.keyboard.press('Tab');await page.getByRole('button',{name:'Seek backward 10 seconds',exact:true}).focus();await page.waitForTimeout(3200);
    assert(await page.locator('.shaka-bottom-controls').isVisible(),'focused playing controls remain visible');
    await page.locator('video').evaluate(video=>video.pause());
    await page.getByRole('button',{name:'Full screen',exact:true}).click();await page.waitForFunction(()=>!!document.fullscreenElement);await page.evaluate(()=>document.activeElement.blur());await page.mouse.move(500,300);await page.waitForTimeout(3200);
    assert(await page.locator('.shaka-bottom-controls').isVisible(),'paused fullscreen controls remain visible');
    await page.screenshot({path:path.join(out,'paused-fullscreen-1280.png')});
    await page.getByRole('button',{name:'Exit full screen',exact:true}).click();
   }
   await context.close();current=null;
  }
  if(((!layoutOnly&&!phoneRefinement)||trackMenus)&&!assFit&&!menuFlow){
   const context=await browser.newContext({viewport:{width:390,height:844},hasTouch:true,isMobile:true,reducedMotion:'reduce'});
   const page=await context.newPage();current=page;
   await page.addInitScript(()=>{const play=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(...args){this.muted=true;return play.apply(this,args);};});
   await page.goto(base+'/review/sign-in?title=tv');
   await page.waitForFunction(()=>!document.querySelector('[data-player-timeline]').disabled);
   await page.locator('video').evaluate(video=>video.pause());
   const next=page.locator('[data-player-next]');
   const href=await next.getAttribute('href');assert.match(href,/\/plus\/watch\/title\/tv\/100\/1\/2\?/,'real fixture resolves next episode in the same TV series');
   assert.equal(await next.getAttribute('aria-disabled'),'false');assert.equal(await next.locator('[data-next-label]').textContent(),'Next episode');
   assert(await next.locator('[data-next-label]').isVisible());
   if(trackMenus){
    assert.equal(await page.getByRole('link',{name:'Next episode',exact:true}).count(),1,'only the in-player Next is initially visible');
    const navigation=page.locator('[data-player-episode-navigation]');assert.equal(await navigation.getAttribute('open'),null);
    await navigation.locator('summary').focus();await page.keyboard.press('Enter');assert(await page.locator('[data-player-sidebar-next]').isVisible(),'native disclosure retains optional adjacent navigation');
    await page.keyboard.press('Enter');assert.equal(await page.getByRole('link',{name:'Next episode',exact:true}).count(),1);
    assert.equal(await page.getByRole('link',{name:'See episodes',exact:false}).count(),1);
   }
   await page.screenshot({path:path.join(out,'next-episode-390.png')});
   report.nextEpisode={href,visibleLabel:true};
   await Promise.all([page.waitForURL(url=>url.pathname==='/plus/watch/title/tv/100/1/2'),next.tap()]);
   await context.close();current=null;
   if(trackMenus){
    const fallback=await browser.newContext({viewport:{width:390,height:844},javaScriptEnabled:false}),p=await fallback.newPage();
    await p.goto(base+'/review/sign-in?title=tv');await p.locator('[data-player-episode-navigation] > summary').click();
    assert(await p.locator('[data-player-sidebar-next]').isVisible(),'adjacent episode navigation remains usable without JavaScript');
    await p.goto(base+'/plus/watch/title/tv/100/1/2');await p.locator('[data-player-episode-navigation] > summary').click();
    assert(await p.locator('[data-player-previous]').isVisible(),'Previous remains available without JavaScript');
    report.noScriptNavigation=true;await fallback.close();
   }
  }
  assert.deepEqual(report.errors,[]);report.passed=true;
 }catch(error){report.passed=false;report.failure=error.message;if(current)await current.screenshot({path:path.join(out,'failure.png')}).catch(()=>{});}
 finally{report.sourceAfter=hashes();report.sourceStable=JSON.stringify(report.sourceBefore)===JSON.stringify(report.sourceAfter);report.passed=report.passed&&report.sourceStable;await browser.close();fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,failure:report.failure,states:report.states.length,errors:report.errors,out}));if(!report.passed)process.exitCode=1;}
})();
