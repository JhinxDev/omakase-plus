const {qaRuntime, qaOutput, qaBrowser} = require('./qa-environment.cjs');
/* Frozen actual-template QA in isolated headless Edge. Every behavioral write is intercepted. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const runtime = qaRuntime();
const {chromium} = require(require.resolve('playwright', {paths:[runtime]}));
const {default:AxeBuilder} = require(require.resolve('@axe-core/playwright', {paths:[runtime]}));
const base = process.env.OMAKASE_REVIEW_URL || 'http://127.0.0.1:8876';
assert.equal(new URL(base).hostname, '127.0.0.1');
const out = process.env.OMAKASE_QA_OUTPUT || qaOutput('discovery');
fs.mkdirSync(out, {recursive:true});
const routes=['home','library','discover','activity','library/1','discover/title/10?run=1','login','auth/google/account'];
const sources=['src/omakase/plus/static/dashboard.css','src/omakase/plus/static/streaming.css','src/omakase/plus/static/atmosphere.css','src/omakase/plus/static/dashboard.js','scripts/preview_plus_workspace.py','scripts/check_discovery.cjs',...['base','home','library','title','discover','recommendation','login','google_account'].map(name=>`src/omakase/plus/templates/${name}.html`)];
const identity=Object.fromEntries(sources.map(name=>[name,crypto.createHash('sha256').update(fs.readFileSync(path.resolve(__dirname,'..',name))).digest('hex')]));
const resume=process.argv.includes('--behavior-only');
const prior=resume?JSON.parse(fs.readFileSync(path.join(out,'browser.partial-feedback-harness.json'),'utf8')):null;
if(prior)for(const name of sources.filter(name=>name!=='scripts/check_discovery.cjs'))assert.equal(identity[name],prior.sourceSha256[name],
  'Cannot reuse browser proof after product/fixture changes: '+name);
const results=prior?prior.results.slice(0,prior.results.findIndex(row=>row.check==='discovery-request-and-result-route')):[];
const record=(check,passed,details={})=>results.push({check,passed,...details});
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const overflow=page=>page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
const shot=async(page,name)=>{await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});if([1440,390].includes(page.viewportSize().width))await page.screenshot({path:path.join(out,name+'-viewport.png'),fullPage:false});};
const errors=[];
function observe(page){page.on('pageerror',error=>errors.push(error.message));}
async function accessible(page,details){
  const axe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  const extra=await overflow(page);
  results.push({...details,overflow:extra,errors:errors.splice(0),violations:axe.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}))});
}
async function removeGuard(route){
  assert.equal(route.request().method(),'GET');
  const response=await route.fetch();
  const html=(await response.text()).replace(/<script>\s*document\.addEventListener\('submit'[\s\S]*?<\/script>/,'');
  // This confined fixture context omits connect-src:none so intercepted fetch can run.
  // The server guard remains intact everywhere else; all non-GETs are blocked below.
  await route.fulfill({status:response.status(),contentType:'text/html',body:html});
}
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 let fatal=null;
 try{
  if(!resume){ for(const width of [2560,1440,1024,768,390,320]){
   const context=await browser.newContext({viewport:{width,height:900},reducedMotion:'reduce'});
   const page=await context.newPage();observe(page);
   await page.addInitScript(()=>{
    window.qaVitals={lcp:0,cls:0};
    new PerformanceObserver(list=>{for(const e of list.getEntries())window.qaVitals.lcp=e.startTime;}).observe({type:'largest-contentful-paint',buffered:true});
    new PerformanceObserver(list=>{for(const e of list.getEntries())if(!e.hadRecentInput)window.qaVitals.cls+=e.value;}).observe({type:'layout-shift',buffered:true});
   });
   for(const route of routes){
    await page.goto(`${base}/plus/${route}`,{waitUntil:'networkidle'});
    await shot(page,`${route.replace(/[^a-z0-9]/gi,'_')}-${width}`);
    const lab=await page.evaluate(()=>({...window.qaVitals,domContentLoaded:performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd,transferBytes:performance.getEntriesByType('resource').reduce((sum,r)=>sum+r.transferSize,0)}));
    await accessible(page,{route,width,lab});
    record('atmosphere-loaded',await page.locator('link[href*="atmosphere.css"]').count()===1&&await page.evaluate(()=>getComputedStyle(document.body).backgroundImage.includes('gradient')),{route,width});
    if(route==='home'){
     const geometry=await page.locator('.viewer-activity-list').evaluate(list=>{
      const boundary=list.getBoundingClientRect();return [...list.querySelectorAll('li')].map(row=>{
       const dot=row.querySelector('.event-dot').getBoundingClientRect(),time=row.querySelector('time').getBoundingClientRect(),text=row.querySelector('div').getBoundingClientRect(),rect=row.getBoundingClientRect();
       return {leftInset:dot.left-boundary.left,rightInset:boundary.right-time.right,contained:text.bottom<=rect.bottom-8&&time.bottom<=rect.bottom-8};});
     });
     record('activity-padding',geometry.every(row=>row.leftInset>=15&&row.rightInset>=15&&row.contained),{width,geometry});
    }
    if(route==='library/1'){
     record('truthful-release-quality',await page.getByLabel('Verified release quality').getByText('1080p release',{exact:true}).isVisible()&&await page.getByLabel('Verified release quality').getByText('720p fallback',{exact:true}).isVisible(),{width});
     await page.getByRole('link',{name:'Update watch progress'}).click();
     await page.locator('.title-technical-details > summary').click();
     await accessible(page,{check:'expanded-watch-and-coverage',width});
     if(width===1440||width===390)await shot(page,`title-expanded-${width}`);
    }
    if(route==='login')record('password-and-google-fallback',await page.getByLabel('Email',{exact:true}).isVisible()&&await page.getByLabel('Password',{exact:true}).isVisible()&&await page.getByRole('link',{name:'Continue with Google'}).isVisible(),{width});
    if(route==='auth/google/account')record('google-link-csrf',await page.getByRole('button',{name:'Link Google account'}).isVisible()&&(await page.locator('form[action$="/link"] input[name="csrf_token"]').getAttribute('value'))==='synthetic-no-auth',{width});
   }
   await page.goto(`${base}/plus/discover`);
   await page.getByRole('heading',{name:'The Wind Archive'}).getByRole('link').click();
   record('recommendation-detail-journey',await page.getByRole('heading',{name:'The story'}).isVisible()&&await page.getByRole('button',{name:'Check download availability'}).isVisible(),{width});
   await page.getByRole('link',{name:'Back to your menu'}).click();
   await page.getByRole('radio',{name:/Hidden gems/}).check();await page.getByRole('radio',{name:/Hidden gems/}).focus();await page.keyboard.press('ArrowRight');
   record('keyboard-lane',await page.getByRole('radio',{name:/New seasons/}).isChecked(),{width});
   await page.locator('#run-provider').selectOption('codex');record('codex-controls',await page.locator('#codex-options').isVisible()&&await page.locator('#configured-options').isHidden(),{width});
   await page.locator('#run-provider').selectOption('configured');record('configured-controls',await page.locator('#codex-options').isHidden()&&await page.locator('#configured-options').isVisible(),{width});
   await page.goto(`${base}/plus/discover?fixture=empty`);record('discovery-empty',await page.getByRole('heading',{name:'Fewer guesses. Better evenings.'}).isVisible(),{width,overflow:await overflow(page)});
   await page.goto(`${base}/plus/auth/google/account?fixture=empty`);record('google-disabled-copy',await page.getByText(/Google sign-in is not configured/).isVisible()&&await page.getByRole('button',{name:'Link Google account'}).count()===0,{width});
   await page.goto(`${base}/plus/login?fixture=error`);record('login-error-visible',await page.getByRole('alert').isVisible()&&await page.getByLabel('Password',{exact:true}).isVisible(),{width});
   await page.goto(`${base}/plus/home`);await page.keyboard.press('Tab');await page.keyboard.press('Enter');record('skip-link',await page.evaluate(()=>document.activeElement.id==='main-content'),{width});
   await page.getByLabel('Open account and utilities menu').click();await page.keyboard.press('Escape');record('account-escape',await page.locator('.account-menu').getAttribute('open')===null,{width});
   await context.close();
  }
  // Relevant 200% text-only stress at a 390px layout. Not a browser zoom/device claim.
  const reflowContext=await browser.newContext({viewport:{width:390,height:900},reducedMotion:'reduce'});
  const reflow=await reflowContext.newPage();observe(reflow);
  for(const route of ['library/1','login','auth/google/account']){
   await reflow.goto(`${base}/plus/${route}`,{waitUntil:'networkidle'});
   if(route==='library/1')await reflow.evaluate(()=>{document.querySelector('#watch-tracking').open=true;document.querySelector('.title-technical-details').open=true;});
   await reflow.evaluate(()=>{
    const sizes=[...document.querySelectorAll('body *')].filter(el=>el instanceof HTMLElement&&([...el.childNodes].some(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim())||['INPUT','SELECT','BUTTON'].includes(el.tagName))).map(el=>[el,parseFloat(getComputedStyle(el).fontSize)]);
    for(const [el,size] of sizes)el.style.fontSize=`${size*2}px`;
   });
   await shot(reflow,`text200-${route.replace(/[^a-z0-9]/gi,'_')}-390`);
   await accessible(reflow,{check:'text-200-reflow',route,width:390});
  }
  await reflowContext.close(); }
  const context=await browser.newContext({viewport:{width:1440,height:900},reducedMotion:'reduce'});
  const page=await context.newPage();observe(page);
  let blockedWrites=0;
  await page.route(`${base}/plus/**`,async route=>{
   if(!['GET','HEAD'].includes(route.request().method())){blockedWrites++;await route.abort('blockedbyclient');return;}
   const type=route.request().resourceType();if(type==='document')await removeGuard(route);else await route.continue();
  });
  // Discovery request payload and clean error recovery.
  let posted,failure=false;
  await page.route(`${base}/plus/api/run`,async route=>{
   posted=route.request().postDataJSON();assert.equal(route.request().method(),'POST');assert.equal(route.request().headers()['x-csrf-token'],'synthetic-no-auth');
   await route.fulfill({status:failure?503:200,json:failure?{status:'error',detail:'Your Codex worker is offline.'}:{status:'running',job_id:'fixture-job'}});
  });
  await page.route(`${base}/plus/api/run/status/*`,route=>route.fulfill({json:{status:'ok',run_id:42,recommendations:[]}}));
  await page.goto(`${base}/plus/discover`);await page.locator('#run-provider').selectOption('codex');await page.locator('#run-model').selectOption('gpt-6-astra');await page.locator('#run-mood').fill('A finished, character-driven mystery');await page.locator('#run-max-episodes').selectOption('13');await page.locator('#run-completed').check();await page.locator('#run-count').fill('5');await page.locator('#run-btn').click();await page.waitForURL('**/plus/discover?run=42');
  assert.deepEqual({provider:posted.provider,model:posted.model,max_episodes:posted.max_episodes,completed_only:posted.completed_only,count:posted.count,mood:posted.mood},{provider:'codex',model:'gpt-6-astra',max_episodes:13,completed_only:true,count:5,mood:'A finished, character-driven mystery'});record('discovery-request-and-result-route',true);
  failure=true;await page.locator('#run-btn').click();await page.locator('#run-error').waitFor({state:'visible'});record('offline-error-recovery',await page.locator('#run-btn').isEnabled()&&await page.locator('#run-error').evaluate(node=>node===document.activeElement)&&await page.locator('#loading').isHidden());
  // Playwright locators wait for the new document during held navigation. Observe
  // the old document from its own timer through console, without canceling navigation.
  const observed=new Map();
  page.on('console',message=>{if(message.text().startsWith('OMAKASE_QA_PENDING:')){const data=JSON.parse(message.text().slice('OMAKASE_QA_PENDING:'.length));observed.get(data.kind)?.resolve(data);}});
  const awaitPending=kind=>{const gate=deferred();observed.set(kind,gate);return Promise.race([gate.promise,new Promise((_,reject)=>setTimeout(()=>reject(new Error(`Old-document observation timed out: ${kind}`)),5000))]);};
  await page.goto(`${base}/plus/home`);let navigationGate=deferred();
  await page.route(`${base}/plus/library?qa=delay-nav`,async route=>{await navigationGate.promise;await removeGuard(route);});
  await page.locator('.primary-nav a[href="/plus/library"]').evaluate(a=>a.href+="?qa=delay-nav");
  const navigationPending=awaitPending('navigation'),navigationReset=awaitPending('navigation-reset');
  await page.evaluate(()=>{
    const watch=setInterval(()=>{
      const loader=document.querySelector('#navigation-loading');if(loader.hidden)return;clearInterval(watch);
      console.log('OMAKASE_QA_PENDING:'+JSON.stringify({kind:'navigation',mainVisible:document.querySelector('#main-content').getBoundingClientRect().height>0,heading:document.querySelector('.product-masthead h1').textContent,animation:getComputedStyle(document.querySelector('.navigation-loading-track'),'::after').animationName}));
      window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
      console.log('OMAKASE_QA_PENDING:'+JSON.stringify({kind:'navigation-reset',hidden:loader.hidden}));
    },100);
  });
  const navigationClick=page.locator('.primary-nav a[href$="delay-nav"]').click({noWaitAfter:true});
  const navigationSnapshot=await navigationPending;
  record('delayed-navigation-main-visible',navigationSnapshot.mainVisible&&navigationSnapshot.heading==='Your evening, queued.',{observation:'Native delayed navigation, old-document timer',snapshot:navigationSnapshot});
  record('reduced-motion-navigation',navigationSnapshot.animation==='none');
  record('pageshow-navigation-reset',(await navigationReset).hidden);
  navigationGate.resolve();await navigationClick;await page.waitForURL('**/plus/library?qa=delay-nav');
  // Native POST body is captured before the network. Observe pending and duplicate
  // semantics inside the actual old document while that POST response is held.
  await page.goto(`${base}/plus/library/1`);await page.getByRole('link',{name:'Update watch progress'}).click();
  const button=page.locator('#watch-tracking form[action$="/next"] button');await button.evaluate(el=>{el.name='watch_action';el.value='next_episode';});
  let formGate=deferred(),nativePosts=[];
  await page.route(`${base}/plus/library/1/watch/next`,async route=>{assert.equal(route.request().method(),'POST');nativePosts.push(route.request().postData());await formGate.promise;await route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><html lang="en"><title>Mocked receipt</title><body><main><h1>Mocked saved</h1></main></body></html>'});});
  const nativePending=awaitPending('native'),nativeReset=awaitPending('native-reset');
  await page.evaluate(()=>{
    const watch=setInterval(()=>{
      const loader=document.querySelector('#navigation-loading'),button=document.querySelector('#watch-tracking form[action$="/next"] button');if(loader.hidden||!button.form.dataset.pendingSubmit)return;clearInterval(watch);
      const event=new SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:button});const duplicateBlocked=!button.form.dispatchEvent(event);
      console.log('OMAKASE_QA_PENDING:'+JSON.stringify({kind:'native',mainVisible:document.querySelector('#main-content').getBoundingClientRect().height>0,busy:button.getAttribute('aria-busy'),disabled:button.disabled,animation:getComputedStyle(button,'::before').animationName,duplicateBlocked}));
      window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
      console.log('OMAKASE_QA_PENDING:'+JSON.stringify({kind:'native-reset',busy:button.getAttribute('aria-busy'),ariaDisabled:button.getAttribute('aria-disabled'),text:button.textContent,pending:!!button.form.dataset.pendingSubmit}));
    },100);
  });
  const nativeClick=button.click({noWaitAfter:true});const nativeSnapshot=await nativePending;
  record('native-post-pending',nativeSnapshot.mainVisible&&nativeSnapshot.busy==='true'&&!nativeSnapshot.disabled,{snapshot:nativeSnapshot});
  record('native-submitter-preserved',new URLSearchParams(nativePosts[0]).get('watch_action')==='next_episode'&&new URLSearchParams(nativePosts[0]).get('csrf_token')==='synthetic-no-auth');
  record('reduced-motion-button',nativeSnapshot.animation==='none');
  record('native-post-duplicate-prevented',nativeSnapshot.duplicateBlocked&&nativePosts.length===1);
  const resetSnapshot=await nativeReset;record('pageshow-form-reset',resetSnapshot.busy===null&&resetSnapshot.ariaDisabled===null&&resetSnapshot.text==='Mark episode 5 watched'&&!resetSnapshot.pending);
  formGate.resolve();await nativeClick;await page.getByRole('heading',{name:'Mocked saved'}).waitFor();
  // Save-for-later and feedback mocked pending, success and retry states.
  for(const action of ['plan','feedback']){
   for(const state of ['error','success']){
    await page.goto(`${base}/plus/discover`);const gate=deferred();let calls=0,body;
    await page.route(`${base}/plus/api/${action}`,async route=>{calls++;assert.equal(route.request().method(),'POST');assert.equal(route.request().headers()['x-csrf-token'],'synthetic-no-auth');body=route.request().postDataJSON();await gate.promise;await route.fulfill({status:state==='error'?503:200,json:state==='error'?{status:'error',detail:'Synthetic save unavailable'}:{status:'ok',local_saved:true}});});
    const target=page.locator(action==='plan'?'.btn-plan':'.btn-feedback[data-feedback="not_for_me"]').first();const group=target.locator('xpath=..');const article=target.locator('xpath=ancestor::article');const articleElement=await article.elementHandle();const original=await target.textContent();await target.click();
    record(`${action}-${state}-pending`,await target.isDisabled()&&await target.getAttribute('aria-busy')==='true'&&await target.textContent()==='Saving…');
    await target.evaluate(el=>el.click());record(`${action}-${state}-duplicate-blocked`,calls===1);
    record(`${action}-${state}-canonical-payload`,action==='plan'?Number.isInteger(body.anilist_id)&&body.anilist_id>0:body.feedback_type==='not_for_me'&&Number.isInteger(body.media_id));
    gate.resolve();
    if(state==='error'){
     await group.locator('.feedback-status[data-state="error"]').waitFor();record(`${action}-error-recovery`,await target.isEnabled()&&await target.getAttribute('aria-busy')===null&&await target.textContent()===original);
     await accessible(page,{check:`${action}-error-accessibility`,width:1440});
    }else if(action==='plan'){
     await group.locator('.feedback-status[data-state="success"]').waitFor();record('plan-success-clean',await target.textContent()==='Saved to watchlist'&&await target.isDisabled()&&await target.getAttribute('aria-busy')===null);
    }else{await page.waitForFunction(el=>!el.isConnected,articleElement);record('feedback-success-clean',await articleElement.evaluate(el=>!el.isConnected));}
    await page.unroute(`${base}/plus/api/${action}`);
   }
  }
  record('source-frozen',sources.every(name=>identity[name]===crypto.createHash('sha256').update(fs.readFileSync(path.resolve(__dirname,'..',name))).digest('hex')));
  record('all-writes-confined',blockedWrites===0,{unexpectedWritesBlocked:blockedWrites});
  record('behavior-page-errors',errors.length===0,{errors:errors.splice(0)});
  await context.close();
 }catch(error){fatal={message:error.message,stack:error.stack};record('suite-completed',false,{fatal});}
 finally{
  await browser.close();
  const failures=results.filter(r=>r.passed===false||r.overflow>0||r.violations?.length||r.errors?.length);
  const receipt={label:'Frozen current actual templates; synthetic data and mocked writes; isolated headless Edge; local lab only',createdAt:new Date().toISOString(),reusedPriorReceipt:prior?{path:'browser.partial-feedback-harness.json',createdAt:prior.createdAt,retainedChecks:prior.results.findIndex(row=>row.check==='discovery-request-and-result-route'),reason:'Fixed dynamic feedback locator after removed article; product and fixtures unchanged'}:null,base,browser:'Microsoft Edge',sourceSha256:identity,completed:!fatal,checks:results.length,failures,results};
  fs.writeFileSync(path.join(out,'browser.json'),JSON.stringify(receipt,null,2));
  console.log(JSON.stringify({checks:results.length,completed:!fatal,failures,output:out}));
  if(failures.length)process.exitCode=1;
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
