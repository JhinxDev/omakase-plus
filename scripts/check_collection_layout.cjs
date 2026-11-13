const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
/* Actual templates and CSS in isolated Edge. All requests intercepted, no providers/server. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');
const runtime = qaRuntime();
const {chromium} = require(require.resolve('playwright', {paths:[runtime]}));
let AxeBuilder;
try { AxeBuilder=require(require.resolve('@axe-core/playwright',{paths:[runtime]})).default; } catch { /* Optional browser-QA dependency. */ }
const root = path.resolve(__dirname, '..');
const selectedWidth=Number(process.argv.find(value=>value.startsWith('--width='))?.slice(8))||null;
const output = process.env.OMAKASE_COLLECTION_QA_OUTPUT || qaOutput('collection_layout');
fs.mkdirSync(output, {recursive:true});
const fixture = `import json, runpy
from pathlib import Path
suite=runpy.run_path('tests/plus/test_streaming_templates.py')
tv=runpy.run_path('tests/plus/test_tv_templates.py')
from omakase.plus.movie_presentation import present_movie
titles=['Code Geass: Akito the Exiled - The Wyvern Arrives','Code Geass: Akito the Exiled - The Wyvern Divided','BLEACH: Thousand-Year Blood War - The Calamity','A Very Long Four Line Title That Must Stay Inside Its Own Reserved Heading Block Even On A Narrow Phone Screen']
context=suite['library_context']()
context['titles']=[{**suite['title_context'](),'id':i+1,'title':t,'required':27,'usable':0,'plex_visible':0,'release_qualities':['1080p']} for i,t in enumerate(titles)]
movies=[present_movie({'imdb_id':f'tt{i+1:07d}','title':t,'year':1982,'state':'ready','plex_present':True,'resolution':'1080p','history':{'status':'completed','status_source':'manual','rating':8},'artwork_url':''}) for i,t in enumerate(['1408','The Thing',*titles])]
shows=[tv['show'](tmdb_id=i+1,title=t,in_plex_episodes=38) for i,t in enumerate(['True Detective','A Series',*titles])]
print(json.dumps({'library':suite['render']('library.html',**context),'movies':suite['render']('movies.html',cards=movies,query='',view='all'),'tv':tv['render']('tv.html',cards=shows,query='',view='all')}))`;
const html = JSON.parse(execFileSync(qaPython(), ['-c',fixture], {cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
const staticRoot = path.join(root,'src/omakase/plus/static');
const evidence={sourceSha256:{},checks:[]};
for(const name of ['cinema.css','streaming.css','tv.css']) evidence.sourceSha256[name]=crypto.createHash('sha256').update(fs.readFileSync(path.join(staticRoot,name))).digest('hex');
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 try {
  for(const scenario of [{width:320},{width:390},{width:390,textScale:2},{width:390,noScript:true},{width:1440},{width:2560},{width:1440,textScale:2}].filter(value=>!selectedWidth||value.width===selectedWidth)) {
   const suffix=`${scenario.textScale?'-text200':''}${scenario.noScript?'-noscript':''}`;
   const context=await browser.newContext({viewport:{width:scenario.width,height:scenario.width<=390?844:1000},reducedMotion:'reduce',javaScriptEnabled:!scenario.noScript});
   const shells=[];
   for(const kind of ['library','movies','tv']) {
    const page=await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',route=>{
     const url=new URL(route.request().url());
     if(url.pathname===`/plus/${kind}`)return route.fulfill({contentType:'text/html',body:html[kind]});
     if(url.pathname.startsWith('/plus/static/')){
      const file=path.resolve(staticRoot,url.pathname.slice('/plus/static/'.length));
      if(file.startsWith(staticRoot+path.sep)&&fs.existsSync(file))return route.fulfill({path:file});
     }
     return route.fulfill({status:404,body:''});
    });
    await page.goto(`http://collection.test/plus/${kind}`,{waitUntil:'networkidle'});
    if(scenario.textScale)await page.addStyleTag({content:'html{font-size:200%}'});
    const searchDisclosure=page.locator('.collection-search-disclosure');
    let folded;
    if(await searchDisclosure.count()) {
     assert.equal(await searchDisclosure.evaluate(node=>node.open),false,'secondary search is folded without JavaScript');
     folded=await page.evaluate(()=>({titles:[...document.querySelectorAll('.movie-card h2,.tv-card h3')].slice(0,2).map(node=>node.getBoundingClientRect().toJSON()),dock:document.querySelector('.mobile-primary-nav').getBoundingClientRect().top}));
     if(scenario.width===390&&!scenario.textScale) assert(folded.titles.every(box=>box.bottom<folded.dock),'first full poster row and titles fit above the phone dock');
     await page.screenshot({path:path.join(output,`${kind}-${scenario.width}${suffix}-folded.png`)});
     const summary=searchDisclosure.locator(':scope > summary');
     await summary.focus();await summary.press('Enter');
     assert.equal(await searchDisclosure.evaluate(node=>node.open),true,'keyboard opens secondary search');
     assert((await summary.boundingBox()).height>=44);
    }
    await page.locator('.watchlist-coverage').evaluateAll(nodes=>nodes.forEach(node=>node.open=true));
    if(kind==='movies') {
     assert.equal(await page.getByText('No viewing record',{exact:true}).count(),0);
     assert.equal(await page.locator('.collection-card-quality').count(),0);
     const alignment=await page.locator('.movie-card-status > .collection-card-availability:only-child').evaluateAll(nodes=>nodes.map(node=>({left:node.getBoundingClientRect().left,parent:node.parentElement.getBoundingClientRect().left})));
     assert(alignment.every(value=>Math.abs(value.left-value.parent)<=1),'a sole availability line aligns with its title');
    }
    if(kind==='tv') {
     assert.equal(await page.locator('.tv-card details').count(),0);
     const inventory=page.locator('#tv-library-details');
     if(await inventory.count()) {
      assert.equal(await inventory.evaluate(node=>node.open),false);
      const before=await page.locator('.tv-card').first().boundingBox();
      const summary=inventory.locator('summary');
      await summary.focus(); await summary.press('Enter');
      assert.equal(await inventory.evaluate(node=>node.open),true);
      assert(await inventory.getByText('Season coverage unknown.',{exact:true}).first().isVisible());
      const after=await page.locator('.tv-card').first().boundingBox();
      assert.equal(after.width,before.width,'inventory disclosure preserves card width');
      assert.equal(after.height,before.height,'inventory disclosure does not stretch browse cards');
      await summary.press('Space');
      assert.equal(await inventory.evaluate(node=>node.open),false);
     }
    }
    const facts=await page.evaluate(()=>{
     const shell=document.querySelector('.collection-search');const style=getComputedStyle(shell);const rect=shell.getBoundingClientRect();
     const controls=[...shell.querySelectorAll('input:not([type=hidden]),select,button')].filter(n=>n.getClientRects().length).map(n=>n.getBoundingClientRect().height);
     const cards=[...document.querySelectorAll('.watchlist-card,.movie-card,.tv-card')].map(node=>{
      const heading=node.querySelector('h2,h3');const meta=node.querySelector('.card-meta-line,.movie-meta,.tv-card-meta');const h=heading.getBoundingClientRect();const m=meta.getBoundingClientRect();const s=getComputedStyle(heading);
      const link=node.querySelector('a');
      const availability=node.querySelector('.collection-card-availability');const progress=node.querySelector('.collection-card-progress,.tv-card-facts .media-progress-meta > span:not(.collection-card-availability)');
      return {title:heading.textContent,headingBottom:h.bottom,nextTop:m.top,clip:s.overflow,lines:h.height/parseFloat(s.lineHeight),name:link.getAttribute('aria-label'),availability:availability?.getBoundingClientRect().toJSON(),progress:progress?.getBoundingClientRect().toJSON(),box:node.getBoundingClientRect().toJSON()};
     });
     const metrics=[...document.querySelectorAll('.watchlist-coverage .coverage-triptych>div')].map(cell=>{
      const label=cell.querySelector('span');const value=cell.querySelector('strong');const range=document.createRange();range.selectNodeContents(label);
      return {label:label.textContent,cell:cell.getBoundingClientRect().toJSON(),ink:range.getBoundingClientRect().toJSON(),value:value.getBoundingClientRect().toJSON()};
     });
     return {metrics,overflow:document.documentElement.scrollWidth-innerWidth,shell:{x:rect.x,width:rect.width,padding:style.padding,border:style.border,bg:style.backgroundColor,radius:style.borderRadius},controls,cards};
    });
    for(const metric of facts.metrics){
     assert(metric.ink.left>=metric.cell.left-1&&metric.ink.right<=metric.cell.right+1,`expanded ${metric.label} text stays within its metric cell`);
     assert(metric.ink.bottom<=metric.value.top+1,`expanded ${metric.label} label stays above its count`);
    }
    assert(facts.overflow<=1,`${kind} ${scenario.width}: horizontal overflow ${facts.overflow}`);
    assert(facts.controls.every(h=>h>=47.5),`${kind}: shared 48px controls ${JSON.stringify(facts.controls)}`);
    for(const card of facts.cards){assert(card.nextTop>=card.headingBottom-1,`${kind}: title/status overlap ${card.title}`);assert.equal(card.clip,'hidden');if(kind==='library')assert(Math.abs(card.lines-3)<.1,'anime title retains reserved lines');else {assert(card.lines<=3.1,'browse title clips after three natural lines');assert(card.nextTop-card.headingBottom<=8,'metadata stays within 8px of its title');if(card.progress&&card.availability)assert(card.availability.top>=card.progress.bottom-1,'availability follows viewing history without a cramped split');}assert(card.name.includes(card.title),'full accessible title');}
    for(const a of facts.cards)for(const b of facts.cards){if(a===b)continue;const x=Math.min(a.box.right,b.box.right)-Math.max(a.box.left,b.box.left);const y=Math.min(a.box.bottom,b.box.bottom)-Math.max(a.box.top,b.box.top);assert(x<=1||y<=1,'expanded cards do not collide');}
    const query=page.locator('.collection-search-query input');
    await query.focus();await query.press('Tab');
    const next=await page.evaluate(()=>document.activeElement.tagName);
    assert.equal(next,'BUTTON','natural primary search keyboard order');
    const disclosure=page.locator('.watchlist-filter-details>summary,.tv-card-inventory>summary').first();
    if(await disclosure.count()){
     await disclosure.focus();const open=await disclosure.evaluate(n=>n.parentElement.open);
     await disclosure.press('Space');assert.equal(await disclosure.evaluate(n=>n.parentElement.open),!open);
     await disclosure.press('Space');
     assert((await disclosure.boundingBox()).height>=44,'touch-sized disclosure');
    }
    assert.deepEqual(errors,[]);
    let accessibility;
    if(AxeBuilder&&kind!=='library'&&!scenario.textScale&&!scenario.noScript&&[390,1440].includes(scenario.width)) {
     const results=await new AxeBuilder({page}).include('.collection-masthead').include('.product-main').analyze();
     accessibility={violations:results.violations.map(({id,impact,nodes})=>({id,impact,targets:nodes.map(node=>node.target)}))};
     assert.deepEqual(accessibility.violations,[],`${kind}: browse accessibility smoke`);
    }
    shells.push(facts.shell);
    if(await searchDisclosure.count()) {
     await page.screenshot({path:path.join(output,`${kind}-${scenario.width}${suffix}-search-expanded.png`),fullPage:true});
     await searchDisclosure.locator(':scope > summary').press('Space');
     assert.equal(await searchDisclosure.evaluate(node=>node.open),false,'keyboard closes secondary search');
    }
    await page.evaluate(()=>{document.activeElement.blur();window.scrollTo(0,0);});
    if(!scenario.noScript)await page.evaluate(()=>new Promise(requestAnimationFrame));
    const shot=`${kind}-${scenario.width}${suffix}`;
    await page.screenshot({path:path.join(output,`${shot}.png`),fullPage:true});
    await page.screenshot({path:path.join(output,`${shot}-viewport.png`)});
    evidence.checks.push({kind,...scenario,folded,accessibility,...facts});
    await page.close();
   }
   for(const shell of shells.slice(1))assert.deepEqual(shell,shells[0],'same collection shell geometry and appearance');
   await context.close();
  }
 } finally {await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(evidence,null,2));}
 console.log(JSON.stringify({passed:evidence.checks.length,output}));
})().catch(error=>{console.error(error);process.exitCode=1;});
