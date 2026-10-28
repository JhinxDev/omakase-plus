const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
/* Read-only actual safe-preview settings and actual-template state fixtures. */
const crypto=require('node:crypto');const fs=require('node:fs');const path=require('node:path');const assert=require('node:assert/strict');const {execFileSync}=require('node:child_process');
const runtime=qaRuntime();const {chromium}=require(require.resolve('playwright',{paths:[runtime]}));
const base=process.env.OMAKASE_REVIEW_URL||'http://127.0.0.1:4418';assert.equal(new URL(base).hostname,'127.0.0.1');const root=path.resolve(__dirname,'..');const output=process.env.OMAKASE_SETTINGS_QA_OUTPUT||qaOutput('settings_surfaces');fs.mkdirSync(output,{recursive:true});
const fixtures=JSON.parse(execFileSync(qaPython(),['-c',"import runpy,json; f=runpy.run_path('tests/plus/test_settings_readability.py'); print(json.dumps({s:f['render_updates'](s) for s in ['current','newer','error','cooldown','disabled']}))"],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
const result={sourceSha256:{},checks:[],limitations:['Update result variants are actual-template fixtures. No update check, settings write, connection check or external navigation was performed.']};
for(const file of ['src/omakase/plus/templates/updates.html','src/omakase/plus/templates/onboarding.html','src/omakase/plus/templates/agents.html','src/omakase/plus/static/refinement.css'])result.sourceSha256[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex');
(async()=>{const browser=await chromium.launch(qaBrowser());try{
 const scenarios=[];for(const width of [1440,390,320])for(const kind of ['updates','agents','onboarding'])scenarios.push({width,kind,state:'actual'});for(const state of Object.keys(fixtures))scenarios.push({width:390,kind:'updates',state});
 for(const scenario of scenarios){const {width,kind,state}=scenario;const context=await browser.newContext({viewport:{width,height:width<500?844:1000},reducedMotion:'reduce'});const page=await context.newPage();const errors=[];const writes=[];page.on('pageerror',e=>errors.push(e.message));
 const target=kind==='onboarding'?'/plus/onboarding?step=2':`/plus/settings/${kind}`;
 await page.route('**/*',route=>{const req=route.request();const url=new URL(req.url());if(req.method()!=='GET'){writes.push(url.pathname);return route.abort('blockedbyclient');}if(url.origin!==new URL(base).origin)return route.abort('blockedbyclient');if(kind==='updates'&&state!=='actual'&&url.pathname==='/plus/settings/updates')return route.fulfill({contentType:'text/html',body:fixtures[state]});return route.continue();});
 await page.goto(base+target,{waitUntil:'networkidle'});const facts={};
 if(kind==='updates'){
  await page.locator('.updates-content').waitFor({state:'visible',timeout:5000});
  facts.cards=await page.locator('.update-card').evaluateAll(nodes=>nodes.map(n=>{const h=n.querySelector('h2');const style=getComputedStyle(n);return {card:n.getBoundingClientRect().toJSON(),heading:h.getBoundingClientRect().toJSON(),padding:parseFloat(style.paddingLeft)};}));
  for(const card of facts.cards){assert(card.padding>=18,'cards retain inner padding');assert(card.heading.left-card.card.left>=17,'heading separated from card border');assert(card.heading.top-card.card.top>=17,'heading has top breathing room');}
  assert(facts.cards[1].card.top-facts.cards[0].card.bottom>=15,'update cards have a visible gap');
  if(state!=='disabled'){
   const button=page.locator('.update-check-action button');assert((await button.boundingBox()).height>=48,'check target48px');assert.equal(await button.getAttribute('data-busy-label'),'Checking releases…');
   const geometry=await page.evaluate(()=>({status:document.querySelector('.update-result').getBoundingClientRect().bottom,button:document.querySelector('.update-check-action').getBoundingClientRect().top,details:document.querySelector('.update-check-details').getBoundingClientRect().top,actionBottom:document.querySelector('.update-check-action').getBoundingClientRect().bottom}));assert(geometry.button-geometry.status>=15,'check action separated from current status');assert(geometry.details-geometry.actionBottom>=10,'details separated from button');facts.geometry=geometry;
  }
  const summary=page.locator('.update-check-details summary');await summary.focus();await summary.press('Space');assert.equal(await page.locator('.update-check-details').getAttribute('open'),'');
 }else if(kind==='agents'){
  await page.locator('.agents-content').waitFor({state:'visible',timeout:5000});await page.locator('.agents-content details').evaluateAll(nodes=>nodes.forEach(n=>n.open=true));
  facts.copy=await page.locator('.agents-content .settings-explainer p').evaluateAll(nodes=>nodes.map(n=>({font:parseFloat(getComputedStyle(n).fontSize),line:parseFloat(getComputedStyle(n).lineHeight)})));assert(facts.copy.every(c=>c.font>=14&&c.font<=16),'agent instructional copy14-16px');
  facts.commands=await page.locator('.agent-command').evaluateAll(nodes=>nodes.map(n=>({text:n.innerText,overflow:getComputedStyle(n).overflowX,whiteSpace:getComputedStyle(n).whiteSpace,font:getComputedStyle(n).fontFamily,tab:n.tabIndex,box:n.getBoundingClientRect().toJSON(),scroll:n.scrollWidth,client:n.clientWidth})));for(const command of facts.commands){assert.equal(command.overflow,'auto');assert.equal(command.whiteSpace,'pre');assert(/mono|Consolas/i.test(command.font));assert.equal(command.tab,0);assert(command.box.left>=0&&command.box.right<=width+1,'command scroll area contained');}
  await page.locator('.agent-command').first().focus();await page.keyboard.press('ArrowRight');
 }else{
  const link=page.getByRole('link',{name:'Quickstart guide'});assert.equal(await link.getAttribute('href'),'https://omakase-plus.jhinx.dev/docs/quickstart');assert(await page.getByText('Movie and TV catalog search needs a configured Seerr connection; Plex stays optional.',{exact:false}).count());await link.focus();facts.quickstart=await link.getAttribute('href');
 }
 facts.overflow=await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);assert(facts.overflow<=1,'no horizontal page overflow');assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
 const area=kind==='updates'?page.locator('.updates-content'):kind==='agents'?page.locator('.agents-content'):page.locator('.onboarding-panel');await area.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,`${kind}-${state}-${width}.png`),fullPage:false});result.checks.push({...scenario,...facts,errors,writes});await context.close();
 }
}finally{await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(result,null,2));}console.log(JSON.stringify({passed:result.checks.length,output}));})().catch(error=>{console.error(error);process.exitCode=1;});
