const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
/* Actual safe-preview recommendation layout. No POST is sent to the server. */
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');const path=require('node:path');const assert=require('node:assert/strict');const crypto=require('node:crypto');
const runtime=qaRuntime();
const {chromium}=require(require.resolve('playwright',{paths:[runtime]}));
const base=process.env.OMAKASE_REVIEW_URL||'http://127.0.0.1:4418';assert.equal(new URL(base).hostname,'127.0.0.1');
const output=process.env.OMAKASE_FEEDBACK_QA_OUTPUT||qaOutput('recommendation_feedback');fs.mkdirSync(output,{recursive:true});
const root=path.resolve(__dirname,'..');
const selectedState=process.argv.find(value=>value.startsWith('--state='))?.slice(8);
const intakeHtml=execFileSync(qaPython(),['-c', "import runpy; f=runpy.run_path('tests/plus/test_recommendation_feedback_ui.py'); print(f['render'](title='Food Wars! The Second Plate', predicted_score=8.4))"],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}});const report={sourceSha256:{},checks:[],limitations:['Read-only layout and keyboard proof. No history, watchlist, intake or provider mutation was submitted.']};
for(const file of ['src/omakase/plus/templates/recommendation.html','src/omakase/plus/static/feedback.css'])report.sourceSha256[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex');
(async()=>{const browser=await chromium.launch(qaBrowser());try{
 for(const state of ['existing-library','intake'].filter(value=>!selectedState||value===selectedState))for(const width of [1440,390,320]){
  const context=await browser.newContext({viewport:{width,height:width<500?844:1000},reducedMotion:'reduce'});const page=await context.newPage();const errors=[];const writes=[];let detailPath='';
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',route=>{const request=route.request();const url=new URL(request.url());if(request.method()!=='GET'){writes.push({method:request.method(),path:url.pathname});return route.abort('blockedbyclient');}if(url.origin!==new URL(base).origin)return route.abort('blockedbyclient');if(state==='intake'&&url.pathname===detailPath)return route.fulfill({contentType:'text/html',body:intakeHtml});return route.continue();});
  await page.goto(base+'/plus/demo/routes',{waitUntil:'networkidle'});const map=JSON.parse(await page.locator('body').innerText());const url=map.get_journeys.find(value=>value.startsWith('/plus/discover/title/'));assert(url,'safe preview lists a recommendation detail');
  detailPath=new URL(base+url).pathname;await page.goto(base+url,{waitUntil:'networkidle'});
  const panel=page.locator('.recommendation-history-panel');await panel.waitFor({state:'visible',timeout:5000});
  assert.equal(await page.locator('link[href*="feedback.css"]').count(),1,'new CSS is actually included');
  if(state==='intake'){assert.equal(await page.locator('.recommendation-primary-actions form[action="/plus/add/preview"]').count(),1);assert.equal(await page.locator('h1').innerText(),'Food Wars! The Second Plate');}
  const summary=panel.locator('summary');await summary.focus();await summary.press('Space');assert.equal(await panel.locator('details').getAttribute('open'),'');
  await summary.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.matches('[data-feedback-score]')),true,'keyboard reaches optional rating');
  await page.locator('[data-feedback-score]').selectOption('8');await page.locator('[data-feedback-score]').press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.dataset.feedback),'already_watched','keyboard reaches watched save');
  const facts=await page.evaluate(()=>{
   const panel=document.querySelector('.recommendation-history-panel');const details=panel.querySelector('details');const rating=panel.querySelector('[data-feedback-score]');const save=panel.querySelector('[data-feedback="already_watched"]');const quiet=panel.querySelector('.recommendation-feedback-quiet');const chip=document.querySelector('.recommendation-availability');
   return {overflow:document.documentElement.scrollWidth-innerWidth,panel:panel.getBoundingClientRect().toJSON(),details:details.getBoundingClientRect().toJSON(),position:getComputedStyle(details).position,rating:rating.getBoundingClientRect().toJSON(),save:save.getBoundingClientRect().toJSON(),quiet:quiet.getBoundingClientRect().toJSON(),chip:chip.getBoundingClientRect().toJSON(),copy:document.querySelector('.recommendation-detail-copy').getBoundingClientRect().toJSON(),controls:[...panel.querySelectorAll('button,summary,select')].filter(n=>n.getBoundingClientRect().height).map(n=>({tag:n.tagName,height:n.getBoundingClientRect().height,width:n.getBoundingClientRect().width})),title:document.querySelector('h1').textContent};
  });
  assert(facts.overflow<=1,'no page horizontal overflow');assert.equal(facts.position,'static','history expands in normal flow');
  assert(facts.details.left>=facts.panel.left&&facts.details.right<=facts.panel.right,'history fits its panel');assert(facts.rating.left>=facts.panel.left&&facts.rating.right<=facts.panel.right,'rating fits its panel');assert(facts.save.left>=facts.panel.left&&facts.save.right<=facts.panel.right,'save button fits its panel');
  assert(facts.quiet.top>=facts.details.bottom,'quiet feedback follows expanded history');assert(facts.controls.every(value=>value.height>=48),'all feedback controls have 48px targets');
  const overlapX=Math.min(facts.rating.right,facts.save.right)-Math.max(facts.rating.left,facts.save.left);const overlapY=Math.min(facts.rating.bottom,facts.save.bottom)-Math.max(facts.rating.top,facts.save.top);assert(overlapX<=0||overlapY<=0,'rating and save never overlap');
  assert(facts.chip.width<Math.max(facts.panel.width,280),'availability remains a compact chip');assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  await panel.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,`feedback-${state}-${width}.png`),fullPage:false});
  await summary.focus();await summary.press('Space');assert.equal(await panel.locator('details').getAttribute('open'),null,'keyboard closes history');
  report.checks.push({state,width,...facts,errors,writes});await context.close();
 }
}finally{await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(report,null,2));}
console.log(JSON.stringify({passed:report.checks.length,output}));})().catch(error=>{console.error(error);process.exitCode=1;});
