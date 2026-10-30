/* Q9 display/interaction checks on finite read-only actual-template fixtures. */
const {qaRuntime,qaOutput,qaBrowser}=require('./qa-environment.cjs');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {chromium}=require(require.resolve('playwright',{paths:[qaRuntime()]}));
const base=process.env.OMAKASE_REVIEW_URL||'http://127.0.0.1:4419';assert.equal(new URL(base).hostname,'127.0.0.1');
const output=process.env.OMAKASE_EPISODE_QA_OUTPUT||qaOutput('episode_rechecks');fs.mkdirSync(output,{recursive:true});
const requestedStates=(process.argv.find(value=>value.startsWith('--state='))?.slice(8)||'').split(',').filter(Boolean);
const states=['complete-fallback','resurfaced','download-pending','publication-pending','no-result','busy','upgrades-unavailable'].filter(state=>!requestedStates.length||requestedStates.includes(state));
const receipt={url:base,source:{},scenarios:[],limitations:['Synthetic read-only presentation fixtures verify rendering and busy JavaScript, not provider jobs, publication, watch persistence or physical devices. Service and authenticated POST evidence is separate.']};
for(const file of ['src/omakase/plus/templates/title.html','src/omakase/plus/templates/_media_components.html','src/omakase/plus/static/episode-states.css','src/omakase/plus/static/dashboard.js','scripts/showcase_preview.py'])receipt.source[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,'..',file))).digest('hex');
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 try{
  for(const width of [1440,390,320])for(const state of states){
   const context=await browser.newContext({viewport:{width,height:width<=390?844:1000},reducedMotion:'reduce'}),page=await context.newPage(),errors=[],writes=[];
   page.on('pageerror',error=>errors.push(error.message));
   await page.route('**/*',route=>{const req=route.request(),url=new URL(req.url());if(req.method()!=='GET'){writes.push(req.method()+' '+url.pathname);return route.abort();}if(url.hostname!=='127.0.0.1')return route.abort();return route.continue();});
   const result={width,state,url:base+'/plus/demo/states/episodes/'+state,failures:[]};
   try{
    await page.goto(result.url,{waitUntil:'networkidle'});
    assert.ok((await page.locator('body').innerText()).includes('Read-only presentation fixture'));
    await page.locator('#watch-tracking').evaluate(element=>element.open=true);
    result.facts=await page.evaluate(()=>{
     const button=document.querySelector('.episode-recheck button'),rect=button.getBoundingClientRect();
     const rows=[...document.querySelectorAll('.viewer-episode')].map(row=>{const quality=row.querySelector('.episode-quality'),name=row.querySelector('h3');return {episode:row.dataset.episode,quality:quality?.textContent.trim(),color:quality&&getComputedStyle(quality).color,box:row.getBoundingClientRect().toJSON(),qualityBox:quality?.getBoundingClientRect().toJSON(),nameLines:name.getBoundingClientRect().height/parseFloat(getComputedStyle(name).lineHeight)};});
     return {overflow:document.documentElement.scrollWidth-innerWidth,heading:document.querySelector('.viewer-episodes .stream-section-heading').getBoundingClientRect().toJSON(),headingTitle:document.querySelector('#episode-heading').getBoundingClientRect().toJSON(),watch:document.querySelector('#watch-tracking').getBoundingClientRect().toJSON(),button:{text:button.textContent.trim(),disabled:button.disabled,box:rect.toJSON()},rows,continueForm:!!document.querySelector('form[action$="/watch/continue"]'),text:document.querySelector('main').innerText};
    });
    const facts=result.facts;assert.ok(facts.overflow<=1,'no horizontal overflow');assert.ok(facts.button.box.height>=44,'touch-sized refresh control');assert.ok(facts.button.box.left>=0&&facts.button.box.right<=width+1,'refresh fits viewport');
    assert.ok(facts.rows.length>0&&facts.rows.every(row=>row.box.left>=0&&row.box.right<=width+1),'episode rows fit viewport');
    assert.ok(facts.rows.filter(row=>row.quality).every(row=>row.qualityBox.left>=row.box.left&&row.qualityBox.right<=row.box.right+1),'quality labels fit their rows');
    assert.ok(facts.rows.every(row=>row.nameLines<=2),'episode names stay readable');
    if(width<=390){assert.ok(facts.button.box.top>=facts.headingTitle.bottom+16,'heading and action stack with breathing room');assert.ok(facts.watch.width>=width*.75,'watch drawer uses available phone width');}
    if(state==='resurfaced'){assert.ok(facts.continueForm);assert.ok(facts.text.includes('Plex availability is unconfirmed'));assert.ok(facts.text.includes('Continue & mark episode 3 watched'));}
    else assert.ok(facts.rows.some(row=>row.quality==='720p fallback release'),'fallback is visible per episode');
    if(state==='busy')assert.ok(facts.button.disabled&&facts.button.text==='Checking episodes…','busy state is truthful and prevents duplicate click');
    if(state==='publication-pending')assert.ok(facts.text.includes('1080p found · publication unconfirmed'));
    if(state==='upgrades-unavailable'){assert.equal(facts.button.text,'Find missing episodes');assert.ok(facts.text.includes('1080p upgrade checks are not enabled'));assert.equal(await page.locator('.episode-recheck input[name="intent"]').inputValue(),'missing');}
    await page.locator('.viewer-episodes').screenshot({path:path.join(output,`${state}-${width}-episodes.png`)});
    if(state==='resurfaced')await page.locator('#watch-tracking').screenshot({path:path.join(output,`${state}-${width}-continue.png`)});
    if(state==='complete-fallback'){
     await page.locator('.episode-recheck').evaluate(form=>form.dispatchEvent(new SubmitEvent('submit',{bubbles:true,cancelable:true})));
     assert.equal(await page.locator('.episode-recheck button').innerText(),'Checking episodes…');assert.ok(await page.locator('.episode-recheck button').isDisabled());
     assert.equal(await page.locator('.episode-recheck').evaluate(form=>form.dispatchEvent(new SubmitEvent('submit',{bubbles:true,cancelable:true}))),false,'repeated submit is stopped');
     await page.evaluate(()=>dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));
     assert.ok(!await page.locator('.episode-recheck button').isDisabled(),'back navigation restores the action');
    }
    assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
   }catch(error){result.failures.push(error.message);await page.screenshot({path:path.join(output,`${state}-${width}-failed.png`)}).catch(()=>{});}
   result.errors=errors;result.writes=writes;receipt.scenarios.push(result);await context.close();
  }
 }finally{await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2));}
 const failures=receipt.scenarios.filter(s=>s.failures.length).map(({width,state,failures})=>({width,state,failures}));process.stdout.write(JSON.stringify({scenarios:receipt.scenarios.length,failed:failures,receipt:path.join(output,'receipt.json')},null,2)+'\n');if(failures.length)process.exitCode=1;
})();
