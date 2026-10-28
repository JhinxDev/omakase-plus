const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
/* Isolated actual-template/CSS regression: synthetic data, blocked network, no writes. */
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const runtime = qaRuntime();
const {chromium} = require(require.resolve('playwright', {paths:[runtime]}));
const output = process.env.OMAKASE_TITLE_QA_OUTPUT || qaOutput('title_details');
fs.mkdirSync(output, {recursive:true});
const fixture = spawnSync(qaPython(), ['-X','utf8','-c', 'import runpy; fixture=runpy.run_path("tests/plus/test_title_episode_states.py"); print(fixture["render_title"]())'], {cwd:root,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
assert.equal(fixture.status,0,fixture.stderr);
const base = fs.readFileSync(path.join(root,'src/omakase/plus/templates/base.html'),'utf8');
const cssFiles = [...base.matchAll(/href="\/plus\/static\/([^?]+\.css)/g)].map(match=>match[1]);
if(!cssFiles.includes('episode-states.css'))cssFiles.push('episode-states.css');
const css = cssFiles.map(file=>fs.readFileSync(path.join(root,'src/omakase/plus/static',file),'utf8')).join('\n');

(async()=>{
 const browser=await chromium.launch(qaBrowser());
 const receipt={fixture:'actual title.html rendered with isolated synthetic progress, media and long release name',styles:cssFiles,viewports:[],limitations:['No live backend, provider, SSH, mutations, physical device or creative acceptance.']};
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
   const outer=page.locator('.title-technical-details > summary');
   await outer.focus();await page.keyboard.press('Enter');
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
    const episode=n=>{const node=document.querySelector(`[data-episode="${n}"]`),s=getComputedStyle(node);return {classes:node.className,background:s.backgroundColor,border:s.borderStyle,outline:s.outlineWidth,text:node.textContent.trim()}};
    const targets=[...document.querySelectorAll('.asset-card h3,.subtitle-summary strong,.subtitle-summary small,.asset-coverage span,.evidence-drawer dd,.repair-list strong,.repair-list span,.title-side .brief-list small')];
    return {overflow:document.documentElement.scrollWidth-innerWidth,episodes:{watched:episode(1),watchedUnverified:episode(2),next:episode(3),verified:episode(4),unverified:episode(5),upcoming:episode(7)},text:targets.map(node=>({text:node.textContent.trim().slice(0,75),fontSize:parseFloat(getComputedStyle(node).fontSize),contrast:contrast(node),width:node.getBoundingClientRect().width})),actions:[...document.querySelectorAll('.repair-list button,.asset-actions button')].map(node=>({text:node.textContent.trim(),height:node.getBoundingClientRect().height})),sourceWhiteSpace:getComputedStyle(document.querySelector('.asset-source')).whiteSpace,episodeActions:document.querySelectorAll('.viewer-episode a,.viewer-episode button').length};
   });
   assert.ok(facts.overflow<=1,`overflow at ${width}: ${facts.overflow}`);
   assert.notEqual(facts.episodes.watched.background,facts.episodes.verified.background,'watched surface differs from unwatched media');
   assert.equal(facts.episodes.verified.background,facts.episodes.unverified.background,'verified does not fill neutral card');
   assert.equal(facts.episodes.watched.background,facts.episodes.watchedUnverified.background,'watched style does not depend on media');
   assert.equal(facts.episodes.next.outline,'1px');
   assert.equal(facts.episodes.upcoming.border,'dashed');
   for(const text of facts.text){assert.ok(text.fontSize>=14,`${text.text}: type ${text.fontSize}`);assert.ok(text.contrast>=4.5,`${text.text}: contrast ${text.contrast}`)}
   for(const action of facts.actions)assert.ok(action.height>=44,`${action.text}: touch target ${action.height}`);
   assert.equal(facts.sourceWhiteSpace,'normal');
   assert.equal(facts.episodeActions,0,'episodes are read-only indicators');
   await page.locator('.asset-actions button').first().focus();
   assert.equal(await page.locator('.asset-actions button').first().evaluate(node=>node===document.activeElement),true);
   await page.screenshot({path:path.join(output,`title-${width}-technical.png`),fullPage:true});
   receipt.viewports.push({width,...facts});
   await context.close();
  }
  fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2));
  console.log(`PASS: ${receipt.viewports.length} widths, independent watch/media states, keyboard disclosures, readable technical evidence/actions. ${output}`);
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
