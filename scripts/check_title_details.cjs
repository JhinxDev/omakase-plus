const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
/* Isolated actual-template/CSS regression: synthetic data, blocked network, no writes. */
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const runtime = qaRuntime();
const {chromium} = require(require.resolve('playwright', {paths:[runtime]}));
let AxeBuilder;
try { AxeBuilder=require(require.resolve('@axe-core/playwright',{paths:[runtime]})).default; } catch { /* Optional browser-QA dependency. */ }
const output = process.env.OMAKASE_TITLE_QA_OUTPUT || qaOutput('title_details');
fs.mkdirSync(output, {recursive:true});
const fixture = spawnSync(qaPython(), ['-X','utf8','-c', 'import runpy; fixture=runpy.run_path("tests/plus/test_title_episode_states.py"); print(fixture["render_title"]())'], {cwd:root,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
assert.equal(fixture.status,0,fixture.stderr);
const playable = spawnSync(qaPython(), ['-X','utf8','-c', `import runpy
fixture=runpy.run_path('tests/plus/test_title_episode_states.py')
title=fixture['title_fixture']()
title.update(play_url='/plus/watch/1/3?play=1',play_episode=3,episode_resume={'percent':28,'minutes_left':17},episode_play_urls={1:'/plus/watch/1/1?play=1',3:'/plus/watch/1/3?play=1',4:'/plus/watch/1/4?play=1'})
print(fixture['render_title'](title))`],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
assert.equal(playable.status,0,playable.stderr);
const base = fs.readFileSync(path.join(root,'src/omakase/plus/templates/base.html'),'utf8');
const cssFiles = [...base.matchAll(/href="\/plus\/static\/([^?]+\.css)/g)].map(match=>match[1]);
if(!cssFiles.includes('episode-states.css'))cssFiles.push('episode-states.css');
cssFiles.push('series-detail.css');
const css = cssFiles.map(file=>fs.readFileSync(path.join(root,'src/omakase/plus/static',file),'utf8')).join('\n');

(async()=>{
 const browser=await chromium.launch(qaBrowser());
 const receipt={fixture:'actual title.html rendered with isolated synthetic progress, media and long release name',styles:cssFiles,source:{},viewports:[],playable:[],limitations:['No live backend, provider, SSH, mutations, physical device or creative acceptance.']};
 for(const file of ['src/omakase/plus/templates/title.html','src/omakase/plus/templates/_series_components.html','src/omakase/plus/static/cinema.css','src/omakase/plus/static/series-detail.css','scripts/check_title_details.cjs'])receipt.source[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex');
 try{
  for(const width of [320,390,1440]){
   const context=await browser.newContext({viewport:{width,height:width<500?844:1000},reducedMotion:'reduce'});
   await context.route('**/*',route=>route.abort());
   const page=await context.newPage();
   await page.setContent(`<html><head><style>${css}</style></head><body class="product-body"><main class="product-main">${fixture.stdout}</main></body></html>`);
   await page.evaluate(()=>{
    const main=document.querySelector('main');const episodes=document.querySelector('.viewer-episodes');const detail=document.querySelector('.title-technical-details');
    main.replaceChildren(episodes,detail);main.style.cssText='margin:0 auto;padding:24px 16px;max-width:1320px;box-sizing:border-box;width:100%';
    document.addEventListener('submit',event=>event.preventDefault());
   });
   const fileDetails = page.locator('.episode-file-details');
   const outer=page.locator('.title-technical-details > summary');
   assert.equal(await page.locator('.viewer-episodes .episode-file-details').count(),0,'file diagnostics share one media disclosure');
   await outer.focus();await page.keyboard.press('Enter');
   assert.equal(await fileDetails.getAttribute('open'),null,'file evidence starts collapsed');
   assert.equal(await fileDetails.locator('.episode-file-row').first().isVisible(),false);
   const episodeHeight = await page.locator('[data-episode="4"]').evaluate(node=>node.getBoundingClientRect().height);
   await fileDetails.locator('summary').focus();
   await page.keyboard.press('Enter');
   assert.equal(await fileDetails.locator('.episode-file-row').first().isVisible(),true,'keyboard reveals all file details');
   assert.equal(await page.locator('[data-episode="4"]').evaluate(node=>node.getBoundingClientRect().height),episodeHeight,'opening file details does not stretch episode cards');
   await page.screenshot({path:path.join(output,`title-${width}-file-details.png`),fullPage:true});
   await page.keyboard.press('Enter');
   assert.equal(await fileDetails.getAttribute('open'),null);
   assert.equal(await page.locator('.title-technical-details').getAttribute('open'),'');
   assert.equal(await page.locator('.asset-source').isVisible(),false,'raw release stays collapsed');
   await page.screenshot({path:path.join(output,`title-${width}-overview.png`),fullPage:true});
   const technical=page.locator('.evidence-drawer > summary');
   await technical.focus();await page.keyboard.press('Enter');
   assert.equal(await page.locator('.asset-source').isVisible(),true,'keyboard opens raw evidence');
   const facts=await page.evaluate(()=>{
    const rgb=text=>(text.match(/[\d.]+/g)||[]).map(Number);
    const luminance=rgb=>rgb.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
    const contrast=node=>{
     let parent=node,bg=[8,11,19];while(parent){const color=rgb(getComputedStyle(parent).backgroundColor);if(color.length===3||color[3]===1){bg=color;break}parent=parent.parentElement}
     const values=[luminance(rgb(getComputedStyle(node).color)),luminance(bg)].sort((a,b)=>b-a);return (values[0]+.05)/(values[1]+.05);
    };
    const episode=n=>{const node=document.querySelector(`[data-episode="${n}"]`),frame=node.querySelector('.series-episode-frame'),state=node.querySelector('.series-episode-state');return {classes:node.className,border:getComputedStyle(frame).borderColor,stateColor:state&&getComputedStyle(state).color,state:state?.textContent.trim(),text:node.textContent.trim()}};
    const targets=[...document.querySelectorAll('.asset-card h3,.subtitle-summary strong,.subtitle-summary small,.asset-coverage span,.evidence-drawer dd,.repair-list strong,.repair-list span,.title-side .brief-list small')];
    return {overflow:document.documentElement.scrollWidth-innerWidth,episodes:{watched:episode(1),watchedUnverified:episode(2),next:episode(3),verified:episode(4),unverified:episode(5),upcoming:episode(7)},text:targets.map(node=>({text:node.textContent.trim().slice(0,75),fontSize:parseFloat(getComputedStyle(node).fontSize),contrast:contrast(node),width:node.getBoundingClientRect().width})),actions:[...document.querySelectorAll('.repair-list button,.asset-actions button')].map(node=>({text:node.textContent.trim(),height:node.getBoundingClientRect().height})),sourceWhiteSpace:getComputedStyle(document.querySelector('.asset-source')).whiteSpace,episodeActions:document.querySelectorAll('.series-episode a,.series-episode button').length};
   });
   assert.ok(facts.overflow<=1,`overflow at ${width}: ${facts.overflow}`);
   assert.equal(facts.episodes.watched.state,'Watched');
   assert.equal(facts.episodes.watchedUnverified.state,'Watched');
   assert.equal(facts.episodes.watched.stateColor,facts.episodes.watchedUnverified.stateColor,'watched styling stays independent of media');
   assert.equal(facts.episodes.verified.state,undefined,'unwatched episodes have no redundant overlay');
   assert.equal(facts.episodes.verified.stateColor,facts.episodes.unverified.stateColor,'media verification stays independent of watching');
   assert.notEqual(facts.episodes.next.border,facts.episodes.verified.border);
   assert.match(facts.episodes.upcoming.text,/Upcoming/);
   for(const text of facts.text){assert.ok(text.fontSize>=14,`${text.text}: type ${text.fontSize}`);assert.ok(text.contrast>=4.5,`${text.text}: contrast ${text.contrast}`)}
   for(const action of facts.actions)assert.ok(action.height>=44,`${action.text}: touch target ${action.height}`);
   assert.equal(facts.sourceWhiteSpace,'normal');
   assert.equal(facts.episodeActions,0,'episodes are read-only indicators');
   const episodePlaceholder = page.locator('[data-episode="1"] .series-episode-brand');
   assert.equal(await episodePlaceholder.innerText(),'Omakase Plus');
   assert.equal(await episodePlaceholder.isVisible(),true,'Pending or missing episode art uses the branded tile');
   await page.locator('.asset-actions button').first().focus();
   assert.equal(await page.locator('.asset-actions button').first().evaluate(node=>node===document.activeElement),true);
   await page.screenshot({path:path.join(output,`title-${width}-technical.png`),fullPage:true});
   receipt.viewports.push({width,...facts});
   await context.close();
  }
  for(const scenario of [{width:320},{width:390},{width:1440},{width:390,touch:true},{width:390,missingArt:true}]) {
   const context=await browser.newContext({viewport:{width:scenario.width,height:scenario.width<500?844:1000},reducedMotion:'reduce',hasTouch:!!scenario.touch,isMobile:!!scenario.touch});
   const page=await context.newPage(),errors=[],writes=[],navigations=[];
   page.on('pageerror',error=>errors.push(error.message));
   await page.route('**/*',route=>{
    const request=route.request(),url=new URL(request.url());
    if(request.method()!=='GET'){writes.push(request.method()+' '+url.pathname);return route.abort();}
    if(url.pathname==='/fixture')return route.fulfill({contentType:'text/html; charset=utf-8',body:`<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style></head><body class="product-body"><main class="product-main">${playable.stdout}</main></body></html>`});
    if(url.pathname==='/plus/series/episodes/anime/1')return route.fulfill({json:Object.fromEntries(Array.from({length:7},(_,i)=>[i+1,{title:`Fixture episode ${i+1}`,artwork:i<5}]))});
    if(url.pathname.startsWith('/plus/series/artwork/')||url.pathname==='/plus/media/21/poster'){
     if(scenario.missingArt)return route.fulfill({status:404,body:''});
     return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270"><rect width="480" height="270" fill="#456b81"/><path d="M0 210 150 70 290 200 390 100 480 170V270H0Z" fill="#233b50"/><circle cx="360" cy="65" r="30" fill="#c3b1ff"/></svg>'});
    }
    if(url.pathname.startsWith('/plus/watch/')){navigations.push(url.pathname+url.search);return route.fulfill({body:'Synthetic playback destination'});}
    if(url.pathname.startsWith('/plus/static/')){
     const file=path.resolve(root,'src/omakase/plus/static',path.basename(url.pathname));
     if(fs.existsSync(file))return route.fulfill({path:file});
    }
    return route.abort();
   });
   await page.goto('http://title.test/fixture');
   await page.evaluate(()=>{
    const main=document.querySelector('main');main.replaceChildren(document.querySelector('.viewer-episodes'),document.querySelector('.title-technical-details'));
    main.style.cssText='margin:0 auto;padding:24px 16px;max-width:1320px;box-sizing:border-box;width:100%';
   });
   await page.addScriptTag({content:fs.readFileSync(path.join(root,'src/omakase/plus/static/history-artwork.js'),'utf8')});
   const episode=page.locator('[data-episode="4"]'),art=episode.locator('.series-episode-image'),cue=episode.locator('.series-episode-art-action svg'),caption=episode.locator('.series-episode-caption-link');
   await episode.scrollIntoViewIfNeeded();
   if(scenario.missingArt) {
    await page.waitForFunction(()=>document.querySelector('[data-episode="4"] .series-episode-image').dataset.artworkScope==='unavailable');
    assert.equal(await episode.locator('[data-history-placeholder]').isVisible(),true);
    assert.equal(await episode.locator('[data-history-placeholder]').getAttribute('aria-hidden'),null);
   }else {
    await page.waitForFunction(()=>{const image=document.querySelector('[data-episode="4"] .series-episode-image');return image.complete&&image.naturalWidth>=16&&!image.hasAttribute('data-artwork-pending');});
    assert.equal(await art.isVisible(),true,'exact episode still loads through real artwork script');
    assert.equal(await art.getAttribute('data-artwork-scope'),'episode','same-origin episode metadata qualifies an exact still');
    assert.equal(await episode.locator('[data-history-artwork-label]').isVisible(),false,'exact still does not carry a series fallback label');
   }
   await page.mouse.move(0,0);await page.evaluate(()=>document.activeElement.blur());
   assert.equal(await cue.evaluate(node=>getComputedStyle(node).opacity),'0','resting still has no persistent play circle');
   assert.equal(await episode.locator('.series-episode-state').count(),0,'unwatched still has no status pill');
   assert.equal(await caption.getAttribute('href'),'/plus/watch/1/4?play=1');
   const captionBox=await caption.boundingBox();
   assert(captionBox.height>=43.9,`caption is a touch-sized playback action: ${JSON.stringify(captionBox)} ${await caption.evaluate(node=>JSON.stringify({display:getComputedStyle(node).display,minHeight:getComputedStyle(node).minHeight}))}`);
   assert.equal(await episode.locator('.series-episode-art-action').getAttribute('tabindex'),'-1');
   const facts=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth-innerWidth,verificationCounts:document.body.textContent.split('3 / 6 episodes verified').length-1,watched:document.querySelector('[data-episode="1"] .series-episode-state').textContent.trim(),resume:document.querySelector('[data-episode="3"] .series-episode-state').textContent.trim(),progress:document.querySelector('[data-episode="3"] progress').value}));
   assert(facts.overflow<=1);assert.equal(facts.verificationCounts,1);assert.equal(facts.watched,'Watched');assert.match(facts.resume,/17 min left/);assert.equal(facts.progress,28);
   const suffix=`${scenario.width}${scenario.touch?'-touch':''}${scenario.missingArt?'-missing-art':''}`;
   await page.locator('.viewer-episodes').screenshot({path:path.join(output,`episodes-${suffix}-rest.png`)});
   if(!scenario.touch) {
    await episode.locator('.series-episode-frame').hover();
    assert.equal(await cue.evaluate(node=>getComputedStyle(node).opacity),'1','hover reveals play cue');
    await page.screenshot({path:path.join(output,`episodes-${suffix}-hover.png`)});
   }
   await page.mouse.move(0,0);await caption.focus();
   assert.equal(await cue.evaluate(node=>getComputedStyle(node).opacity),'1','keyboard focus reveals play cue');
   assert.equal(await caption.evaluate(node=>node===document.activeElement),true);
   await page.screenshot({path:path.join(output,`episodes-${suffix}-keyboard.png`)});
   let accessibility;
   if(AxeBuilder&&!scenario.touch&&!scenario.missingArt) {
    const results=await new AxeBuilder({page}).include('.viewer-episodes').analyze();
    accessibility={violations:results.violations.map(({id,impact,nodes})=>({id,impact,targets:nodes.map(node=>node.target)}))};
    assert.deepEqual(accessibility.violations,[],'episode card accessibility smoke');
   }
   await Promise.all([page.waitForURL('http://title.test/plus/watch/1/4?play=1'),scenario.touch?episode.locator('.series-episode-art-action').tap():caption.press('Enter')]);
   assert.deepEqual(navigations,['/plus/watch/1/4?play=1'],'image/touch or caption/keyboard opens exact episode');
   assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
   receipt.playable.push({...scenario,...facts,accessibility,errors,writes,navigations});await context.close();
  }
  fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2));
  console.log(`PASS: ${receipt.viewports.length} diagnostic widths, ${receipt.playable.length} playable states, exact keyboard/touch targets and unobstructed stills. ${output}`);
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
