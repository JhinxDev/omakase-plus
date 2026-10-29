const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
/* Actual templates and CSS in isolated Edge. All requests intercepted, no providers/server. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');
const runtime = qaRuntime();
const {chromium} = require(require.resolve('playwright', {paths:[runtime]}));
const root = path.resolve(__dirname, '..');
const selectedWidth=Number(process.argv.find(value=>value.startsWith('--width='))?.slice(8))||null;
const output = process.env.OMAKASE_COLLECTION_QA_OUTPUT || qaOutput('collection_layout');
fs.mkdirSync(output, {recursive:true});
const fixture = `import json, runpy
from pathlib import Path
suite=runpy.run_path('tests/plus/test_streaming_templates.py')
tv=runpy.run_path('tests/plus/test_tv_templates.py')
titles=['Code Geass: Akito the Exiled - The Wyvern Arrives','Code Geass: Akito the Exiled - The Wyvern Divided','BLEACH: Thousand-Year Blood War - The Calamity','A Very Long Four Line Title That Must Stay Inside Its Own Reserved Heading Block Even On A Narrow Phone Screen']
context=suite['library_context']()
context['titles']=[{**suite['title_context'](),'id':i+1,'title':t,'required':27,'usable':0,'plex_visible':0,'release_qualities':['1080p']} for i,t in enumerate(titles)]
movies=[{'imdb_id':'tt0084787','title':t,'year':1982,'state':'ready','resolution':'1080p','history':{},'artwork_url':''} for t in titles]
shows=[tv['show'](title=t,in_plex_episodes=38) for t in titles]
print(json.dumps({'library':suite['render']('library.html',**context),'movies':suite['render']('movies.html',cards=movies,query='',view='all'),'tv':tv['render']('tv.html',cards=shows,query='',view='all')}))`;
const html = JSON.parse(execFileSync(qaPython(), ['-c',fixture], {cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
const staticRoot = path.join(root,'src/omakase/plus/static');
const evidence={sourceSha256:{},checks:[]};
for(const name of ['cinema.css','streaming.css','tv.css']) evidence.sourceSha256[name]=crypto.createHash('sha256').update(fs.readFileSync(path.join(staticRoot,name))).digest('hex');
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 try {
  for(const scenario of [{width:320},{width:390},{width:1440},{width:2560},{width:1440,textScale:2}].filter(value=>!selectedWidth||value.width===selectedWidth)) {
   const context=await browser.newContext({viewport:{width:scenario.width,height:1000},reducedMotion:'reduce'});
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
    await page.locator('.watchlist-coverage,.tv-card-inventory').evaluateAll(nodes=>nodes.forEach(node=>node.open=true));
    const facts=await page.evaluate(()=>{
     const shell=document.querySelector('.collection-search');const style=getComputedStyle(shell);const rect=shell.getBoundingClientRect();
     const controls=[...shell.querySelectorAll('input:not([type=hidden]),select,button')].filter(n=>n.getClientRects().length).map(n=>n.getBoundingClientRect().height);
     const cards=[...document.querySelectorAll('.watchlist-card,.movie-card,.tv-card')].map(node=>{
      const heading=node.querySelector('h2,h3');const meta=node.querySelector('.card-meta-line,.movie-meta,.tv-card-meta');const h=heading.getBoundingClientRect();const m=meta.getBoundingClientRect();const s=getComputedStyle(heading);
      const link=node.querySelector('a');
      return {title:heading.textContent,headingBottom:h.bottom,nextTop:m.top,clip:s.overflow,lines:h.height/parseFloat(s.lineHeight),name:link.getAttribute('aria-label'),box:node.getBoundingClientRect().toJSON()};
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
    for(const card of facts.cards){assert(card.nextTop>=card.headingBottom-1,`${kind}: title/status overlap ${card.title}`);assert.equal(card.clip,'hidden');assert(Math.abs(card.lines-3)<.1,'title reserves three lines');assert(card.name.includes(card.title),'full accessible title');}
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
    shells.push(facts.shell);
    await page.screenshot({path:path.join(output,`${kind}-${scenario.width}${scenario.textScale?'-text200':''}.png`),fullPage:true});
    evidence.checks.push({kind,...scenario,...facts});
    await page.close();
   }
   for(const shell of shells.slice(1))assert.deepEqual(shell,shells[0],'same collection shell geometry and appearance');
   await context.close();
  }
 } finally {await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(evidence,null,2));}
 console.log(JSON.stringify({passed:evidence.checks.length,output}));
})().catch(error=>{console.error(error);process.exitCode=1;});
