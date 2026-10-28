/* Actual Connections/AI templates + synthetic loopback POST sink; no provider calls. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require(process.argv[4] || 'playwright');
const fixtures=process.argv[2],output=process.argv[3],staticRoot=path.resolve(__dirname,'../../src/omakase/plus/static');
if(!fixtures || !output)throw Error('Pass fixture directory, output directory, existing Playwright module.');
let current='connections-saved',posts=[];
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://fixture.invalid');
 if(req.method==='POST'){let body='';for await(const chunk of req)body+=chunk;posts.push({path:url.pathname,fields:Object.fromEntries(new URLSearchParams(body))});res.writeHead(200,{'content-type':'text/html'});res.end(fs.readFileSync(path.join(fixtures,current+'.html')));return;}
 if(url.pathname.startsWith('/plus/static/')){const name=url.pathname.slice('/plus/static/'.length),file=path.resolve(staticRoot,name);if(!file.startsWith(staticRoot+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}const type=name.endsWith('.css')?'text/css':name.endsWith('.js')?'application/javascript':name.endsWith('.woff2')?'font/woff2':name.endsWith('.png')?'image/png':'image/svg+xml';res.writeHead(200,{'content-type':type});res.end(name.endsWith('.js')&&name!=='credentials.js'?'':fs.readFileSync(file));return;}
 res.writeHead(200,{'content-type':'text/html'});res.end(fs.readFileSync(path.join(fixtures,current+'.html')));
});
(async()=>{
 fs.mkdirSync(output,{recursive:true});await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin='http://127.0.0.1:'+server.address().port;
 const browser=await chromium.launch({channel:'msedge',headless:true});const receipts=[];
 try{
  for(const width of [1440,390,320])for(const kind of ['connections','ai'])for(const saved of [true,false]){
   current=kind+'-'+(saved?'saved':'empty');posts=[];
   const context=await browser.newContext({viewport:{width,height:900},reducedMotion:'reduce'}),page=await context.newPage(),errors=[];let external=0;
   page.on('pageerror',error=>errors.push(error.name));await page.route('**/*',route=>{if(new URL(route.request().url()).origin!==origin){external++;return route.abort();}return route.continue();});
   await page.goto(origin+(kind==='connections'?'/plus/settings/connections':'/plus/settings/ai'));await page.evaluate(()=>document.fonts.ready);
   const form=page.locator(kind==='connections'?'.connection-form':'.settings-layout');
   if(kind==='connections'&&saved)await page.locator('.connection-editor > summary').click();
   const control=form.locator('[data-credential-control]'),input=control.locator('input[type=password]'),submit=form.locator('[data-credential-submit]');
   assert.equal(await input.inputValue(),'');assert.equal(await input.getAttribute('value'),null);assert.equal(await input.getAttribute('type'),'password');
   if(saved){
    assert(await control.getByText('Key saved securely',{exact:true}).isVisible());assert(await input.isHidden());assert(await input.isDisabled());
    const summary=control.locator('summary');await summary.focus();await page.keyboard.press('Enter');await input.waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('[data-credential-input]').disabled);
    assert.equal(await input.inputValue(),'');assert.equal(await submit.innerText(),'Save replacement key');
    await input.fill('synthetic-replacement-only');await control.getByRole('button',{name:'Cancel',exact:true}).click();
    assert.equal(posts.length,0,'Cancel must not submit or mutate');assert.equal(await input.inputValue(),'');assert(await input.isHidden());assert(await input.isDisabled());assert(await summary.evaluate(el=>el===document.activeElement));
    const omitted=await form.evaluate(el=>!new FormData(el).has(el.querySelector('[data-credential-input]').name));assert(omitted,'Closed replacement must not enter form payload');
    await summary.click();await input.waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('[data-credential-input]').disabled);assert.equal(await input.inputValue(),'');
   }else{assert(await input.isVisible());assert.equal(await control.locator('summary').count(),0);assert.equal(await input.getAttribute('placeholder'),'Paste your key');}
   const box=await input.boundingBox();assert(box.x>=0&&box.x+box.width<=width+1&&box.height>=44);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   await page.screenshot({path:path.join(output,`${current}-${width}.png`),fullPage:true});
   await input.fill('synthetic-replacement-only');await submit.click();
   await page.waitForLoadState('domcontentloaded');for(let attempt=0;attempt<30&&posts.length===0;attempt++)await new Promise(resolve=>setTimeout(resolve,20));
   assert.equal(posts.length,1);assert.equal(posts[0].fields.csrf_token,'synthetic-credential-csrf');assert.equal(posts[0].fields[kind==='connections'?'api_key':'llm_api_key'],'synthetic-replacement-only');assert.equal(posts[0].path,kind==='connections'?'/plus/settings/connections/realdebrid/save':'/plus/settings');
   if(kind==='ai'&&saved){
    const fresh=page.locator('[data-credential-control]');await fresh.locator('summary').click();
    const replacement=fresh.locator('input[type=password]');await page.waitForFunction(()=>!document.querySelector('[data-credential-input]').disabled);
    await replacement.fill('synthetic-removal-wins');await page.getByRole('button',{name:'Remove key',exact:true}).click();await page.waitForLoadState('domcontentloaded');
    for(let attempt=0;attempt<30&&posts.length<2;attempt++)await new Promise(resolve=>setTimeout(resolve,20));
    assert.equal(posts.length,2);assert.equal(posts[1].fields.delete_llm_api_key,'1');assert.equal(posts[1].fields.llm_api_key,'synthetic-removal-wins');
   }
   assert.equal(external,0);assert.deepEqual(errors,[]);receipts.push({width,kind,saved,emptyReplacement:true,cancelNoSubmit:saved,closedFieldOmitted:saved,explicitSubmit:true,formContractsPreserved:true,noOverflow:true,errors:0});await context.close();
  }
  fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({method:'Actual rendered templates/static CSS + credentials controller; synthetic loopback forms only',receipts},null,2)+'\n');console.log(JSON.stringify({result:'PASS',scenarios:receipts.length,widths:[1440,390,320]}));
 }finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);server.closeAllConnections();server.close();process.exit(1);});
