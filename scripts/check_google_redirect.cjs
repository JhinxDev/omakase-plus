const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
/* Real Google router + production middleware via ephemeral stdin ASGI, no listener. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {spawn}=require('node:child_process');const readline=require('node:readline');
const runtime=qaRuntime();
const {chromium}=require(require.resolve('playwright',{paths:[runtime]}));
const {default:AxeBuilder}=require(require.resolve('@axe-core/playwright',{paths:[runtime]}));
const root=path.resolve(__dirname,'..'),out=process.env.OMAKASE_GOOGLE_QA_OUTPUT||qaOutput('google_redirect');fs.mkdirSync(out,{recursive:true});
const sources=['src/omakase/web/server.py','src/omakase/plus/google_auth.py','src/omakase/plus/templates/google_account.html','src/omakase/plus/static/atmosphere.css','src/omakase/plus/static/dashboard.js','tests/plus/google_browser_fixture.py','scripts/check_google_redirect.cjs'];
const hashes=Object.fromEntries(sources.map(file=>[file,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')]));
const pending=new Map();let sequence=0,resolveReady;const ready=new Promise(done=>resolveReady=done);
const fixture=spawn(qaPython(),['-u',path.join(root,'tests/plus/google_browser_fixture.py')],{cwd:root,stdio:['pipe','pipe','pipe']});
let stderr='';fixture.stderr.on('data',data=>stderr+=data.toString());
readline.createInterface({input:fixture.stdout}).on('line',line=>{const data=JSON.parse(line);if(data.kind==='ready')resolveReady(data);else{pending.get(data.id)?.(data);pending.delete(data.id);}});
const rpc=request=>new Promise(done=>{const id=++sequence;pending.set(id,done);fixture.stdin.write(JSON.stringify({...request,id})+'\n');});
const checks=[],record=(name,pass,extra={})=>checks.push({name,pass,...extra});
(async()=>{let browser;let fatal;
try{
 const fixtureReady=await Promise.race([ready,new Promise((_,reject)=>setTimeout(()=>reject(new Error('ASGI fixture did not become ready')),10000))]);
 browser=await chromium.launch(qaBrowser());
 for(const legacy of [true,false]){
  const context=await browser.newContext({viewport:{width:1440,height:900},reducedMotion:'reduce'});
  await context.addCookies([{name:'omakase_session',value:fixtureReady.session,url:'https://omakase.example.invalid/plus',secure:true,httpOnly:true,sameSite:'Lax'}]);
  const page=await context.newPage();let googleRequests=0,linkPosts=0,unknownRequests=0,pageErrors=[],linkStatus=null;

  page.on('pageerror',e=>pageErrors.push(e.name));
  await page.addInitScript(()=>{window.qaCspBlocked=false;document.addEventListener('securitypolicyviolation',event=>{if(event.effectiveDirective==='form-action')window.qaCspBlocked=true;});});
  const cdp=await context.newCDPSession(page);
  // Browser Fetch interception includes redirected hops that Playwright routes omit.
  cdp.on('Fetch.requestPaused',async event=>{
   const native=event.request;const route={request:()=>({url:()=>native.url,method:()=>native.method,headers:()=>Object.fromEntries(Object.entries(native.headers).map(([key,value])=>[key.toLowerCase(),value])),postDataBuffer:()=>native.postData?Buffer.from(native.postData):null}),fulfill:async data=>cdp.send('Fetch.fulfillRequest',{requestId:event.requestId,responseCode:data.status,responseHeaders:Object.entries({...data.headers,...(data.contentType?{'content-type':data.contentType}:{})}).map(([name,value])=>({name,value})),body:Buffer.from(data.body).toString('base64')}),abort:async()=>cdp.send('Fetch.failRequest',{requestId:event.requestId,errorReason:'BlockedByClient'})};
   const request=route.request(),url=new URL(request.url());
   if(url.origin==='https://accounts.google.com'&&url.pathname==='/o/oauth2/v2/auth'){
    googleRequests++;assert.equal(request.method(),'GET');assert.equal(url.searchParams.get('code_challenge_method'),'S256');assert.equal(url.searchParams.get('scope'),'openid');
    assert.ok(url.searchParams.get('state'));assert.ok(url.searchParams.get('nonce'));assert.ok(url.searchParams.get('code_challenge'));
    await route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><html lang="en"><title>Intercepted authorization</title><body><main><h1>Google authorization intercepted</h1><p>Synthetic boundary only. No Google account was contacted.</p></main></body></html>'});return;
   }
   if(url.origin==='https://accounts.google.com'&&url.pathname==='/favicon.ico'){await route.fulfill({status:404,body:''});return;}
   if(url.origin!=='https://omakase.example.invalid'){unknownRequests++;await route.abort('blockedbyclient');return;}
   if(request.method()==='POST'){assert.equal(url.pathname,'/plus/auth/google/link');linkPosts++;}
   const response=await rpc({method:request.method(),path:url.pathname+url.search,headers:request.headers(),body:request.postDataBuffer()?.toString('base64')||''});
   if(url.pathname==='/plus/auth/google/link')linkStatus=response.status;
   const headers=response.headers;delete headers['content-length'];delete headers['transfer-encoding'];delete headers['content-encoding'];
   if(legacy&&url.pathname==='/plus/auth/google/account')headers['content-security-policy']=headers['content-security-policy'].replace("form-action 'self' https://accounts.google.com;","form-action 'self';");
   await route.fulfill({status:response.status,headers,body:Buffer.from(response.body,'base64')});
  });
  await cdp.send('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
  const response=await page.goto('https://omakase.example.invalid/plus/auth/google/account',{waitUntil:'networkidle'});
  const policy=response.headers()['content-security-policy'];
  if(!legacy){
   record('exact-fixed-origin-policy',policy.includes("form-action 'self' https://accounts.google.com;")&&!policy.includes('*')&&policy.includes("script-src 'self';")&&policy.includes("connect-src 'self';"));
   for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:900});
    await page.screenshot({path:path.join(out,`google-account-${width}.png`),fullPage:true});
    const axe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
    const panel=await page.locator('.google-account-heading').evaluate(el=>({padding:getComputedStyle(el).paddingLeft,headingSize:getComputedStyle(el.querySelector('h2')).fontSize}));
    record('account-layout-accessibility',overflow===0&&axe.violations.length===0,{width,overflow,violations:axe.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)})),panel});
   }
   await page.setViewportSize({width:390,height:900});
   await page.evaluate(()=>{const text=[...document.querySelectorAll('.google-account-panel *')].filter(el=>el instanceof HTMLElement&&[...el.childNodes].some(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim())).map(el=>[el,parseFloat(getComputedStyle(el).fontSize)]);for(const [el,size]of text)el.style.fontSize=`${size*2}px`;});
   record('account-text200-reflow',await page.evaluate(()=>document.documentElement.scrollWidth===innerWidth));
   await page.screenshot({path:path.join(out,'google-account-text200-390.png'),fullPage:true});
  }
  const submit=page.getByRole('button',{name:'Link Google account'}).click({noWaitAfter:true});
  if(legacy){await page.waitForFunction(()=>window.qaCspBlocked);record('old-self-only-policy-negative-control',linkPosts===1&&googleRequests===0);}
  else{await page.waitForURL(u=>u.origin==='https://accounts.google.com'&&u.pathname==='/o/oauth2/v2/auth',{timeout:10000}).catch(async error=>{record('redirect-debug',false,{linkPosts,linkStatus,googleRequests,origin:new URL(page.url()).origin,path:new URL(page.url()).pathname});throw error;});await page.getByRole('heading',{name:'Google authorization intercepted'}).waitFor();record('native-post-303-google-redirect',linkPosts===1&&googleRequests===1);}
  await submit;record('no-real-provider-or-unexpected-requests',unknownRequests===0&&pageErrors.length===0,{legacy,unknownRequests,pageErrors});await context.close();
 }
 record('source-frozen',sources.every(file=>hashes[file]===crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')));
}catch(error){fatal=error.message;record('completed',false,{fatal});}
finally{if(browser)await browser.close();fixture.stdin.write(JSON.stringify({stop:true})+'\n');fixture.stdin.end();await new Promise(resolve=>{if(fixture.exitCode!==null)resolve();else fixture.once('exit',resolve);});
 const receipt={label:'Actual Google router and production Plus security middleware; synthetic SQLite/session; native isolated headless Edge; all network intercepted; no real Google account',createdAt:new Date().toISOString(),sourceSha256:hashes,checks,complete:!fatal,failures:checks.filter(c=>!c.pass)};
 fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify(receipt,null,2));console.log(JSON.stringify({checks:checks.length,complete:receipt.complete,failures:receipt.failures,output:out}));if(receipt.failures.length)process.exitCode=1;
}
})().catch(error=>{console.error(error.message);fixture.kill();process.exitCode=1;});
