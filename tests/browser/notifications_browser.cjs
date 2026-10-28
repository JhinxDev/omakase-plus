/* Synthetic browser proof for the actual header/CSS + notification controller.
   Args: rendered synthetic base.html, output directory, existing Playwright module. */
const fs = require('fs');
const path = require('path');
const http = require('http');
const assert = require('assert/strict');
const { chromium } = require(process.argv[4] || 'playwright');
const AxeBuilder = require(path.resolve(process.argv[4] || require.resolve('playwright'), '../@axe-core/playwright')).default;
const product = path.resolve(__dirname, '../../src/omakase/plus');
const output = process.argv[3];
if (!process.argv[2] || !output) throw Error('Pass fixture HTML and output directory.');
const html = fs.readFileSync(process.argv[2], 'utf8').replace('</main>', '<button id="outside-control" type="button">Outside control</button></main>');
const staticRoot = path.join(product, 'static');
const allowed = new Set([...html.matchAll(/\/plus\/static\/([^?"/]+)/g)].map(m => m[1]));
const items = [
 { id:'completed_unrated:anime:101:2026-10-28T06:00:00',kind:'completed_unrated',media_type:'anime',title:'The Garden at the End of a Very Long Adventure',message:'You finished this title. Add your rating whenever you like.',href:'/plus/library/101#watch-tracking',rating:null,read:false,artwork_url:null },
 { id:'removal_reminder:anime:102',kind:'removal_reminder',media_type:'anime',title:'A Quiet Constellation',message:'Completed managed media is saved. Review it when you are ready.',href:'/plus/library/102/remove',rating:8.5,read:false,artwork_url:null },
 { id:'completed_unrated:movie:tt1234567',kind:'completed_unrated',media_type:'movie',title:'The Last Evening',message:'Record your rating for this completed film.',href:'/plus/movies/tt1234567#movie-rating',rating:null,read:false,artwork_url:null },
 { id:'completed_unrated:tv:45',kind:'completed_unrated',media_type:'tv',title:'The Map of Tomorrow',message:'Your series is marked complete.',href:'/plus/tv/45#tv-history',rating:null,read:true,artwork_url:null },
 { id:'completed_unrated:anime:999',kind:'completed_unrated',media_type:'anime',title:'<img src=x onerror=alert(1)>',message:'An unsafe destination must never become a link.',href:'javascript:alert(1)',rating:null,read:true,artwork_url:null },
];
let current = [], mode = 'normal', mutationStatus = 200;
let reads = 0, mutations = [];
let deferNextRead = false, releaseHeldResponse = null;
const serveOnly = process.argv.includes('--serve-only');
const feed = () => ({ items:current,unread_count:current.filter(i => !i.read).length,count:current.length,count_capped:false,limit:20 });
const server = http.createServer((req,res) => {
 const url = new URL(req.url,'http://fixture.invalid');
 if (url.pathname === '/plus/api/notifications') {
  reads++;
  if (mode === 'timeout') return;
  res.writeHead(mode === 'error' ? 503 : mode === 'session' ? 401 : 200,{'content-type':'application/json'});
  res.end(JSON.stringify(feed())); return;
 }
 if (url.pathname.startsWith('/plus/api/notifications/') && req.method === 'POST') {
  mutations.push({path:url.pathname,csrf:req.headers['x-csrf-token']});
  if (mutationStatus !== 200) {res.writeHead(mutationStatus);res.end('{}');return;}
  const match=url.pathname.match(/^\/plus\/api\/notifications\/(.+)\/(read|dismiss)$/);
  const id=decodeURIComponent(match[1]);
  current=match[2]==='dismiss'?current.filter(i=>i.id!==id):current.map(i=>i.id===id?{...i,read:true}:i);
  res.writeHead(200,{'content-type':'application/json'});
  const responseBody=JSON.stringify({ok:true,...feed()});
  if(deferNextRead && match[2]==='read') {deferNextRead=false;releaseHeldResponse=()=>res.end(responseBody);return;}
  res.end(responseBody);return;
 }
 if (url.pathname.startsWith('/plus/static/')) {
  const name=path.basename(url.pathname);
  if (!allowed.has(name)) {res.writeHead(404);res.end();return;}
  const type=name.endsWith('.css')?'text/css':name.endsWith('.js')?'application/javascript':name.endsWith('.woff2')?'font/woff2':'image/svg+xml';
  res.writeHead(200,{'content-type':type});
  // Isolate this feature from unrelated controllers, retaining the full actual CSS.
  res.end(name.endsWith('.js') && name!=='notifications.js'?'':fs.readFileSync(path.join(staticRoot,name)));return;
 }
 res.writeHead(200,{'content-type':'text/html'});res.end(serveOnly?html.replace('<button id="outside-control" type="button">Outside control</button>','<p>Synthetic review: open the bell beside Account to review sample reminders. No production account or provider data is connected.</p>'):html);
});
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 await new Promise(resolve=>server.listen(serveOnly?4435:0,'127.0.0.1',resolve));
 const base='http://127.0.0.1:'+server.address().port;
 if(serveOnly) {current=structuredClone(items.slice(0,4));console.log('SYNTHETIC_REVIEW '+base+'/plus/home');return;}
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const receipts=[];
 async function setup(width,scenario='normal') {
  current=structuredClone(items);mode=scenario;reads=0;mutations=[];mutationStatus=200;
  const context=await browser.newContext({viewport:{width,height:900},reducedMotion:'reduce'});
  const page=await context.newPage();const failures=[];
  page.on('pageerror',error=>failures.push(error.message));
  await page.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
  await page.goto(base+'/plus/home');await page.evaluate(()=>document.fonts.ready);
  return {context,page,failures};
 }
 try {
  for(const width of [1440,2560,390,320]) {
   const {context,page,failures}=await setup(width);
   const toggle=page.locator('[data-notification-toggle]'),panel=page.locator('[data-notification-panel]');
   await page.waitForFunction(()=>!document.querySelector('[data-notification-badge]').hidden);
   assert.equal(reads,1);assert.equal(await toggle.getAttribute('aria-label'),'Notifications, 3 unread');
   const geometry=await page.evaluate(()=>{
    const box=selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
    return {brand:box('.stream-brand'),bell:box('.notification-bell'),account:box('.account-menu summary'),search:box(innerWidth>900?'.header-search':'.header-search-mobile'),overflow:document.documentElement.scrollWidth-innerWidth};
   });
   assert.equal(geometry.overflow,0);assert(geometry.bell.height>=44&&geometry.bell.width>=44);assert(geometry.account.height>=44&&geometry.account.width>=44);
   assert(geometry.brand.right<=geometry.search.left+1);assert(geometry.search.right<=geometry.bell.left+1);assert(geometry.bell.right<=geometry.account.left+1);
   await toggle.focus();await page.keyboard.press('Enter');await panel.waitFor({state:'visible'});
   assert(await page.locator('[data-notification-close]').evaluate(el=>el===document.activeElement));
   assert.equal(await panel.locator('.notification-item').count(),5);
   assert.equal(await panel.locator('a[href^="javascript:"]').count(),0);assert.equal(await panel.locator('img').count(),0);
   assert.equal(await panel.getByText('Your rating 8.5/10',{exact:true}).count(),1);
   assert.equal(await panel.getByRole('link',{name:/Review media:/}).getAttribute('href'),'/plus/library/102/remove');
   const dimensions=await panel.evaluate(el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,bottom:r.bottom,height:r.height,scrollable:el.querySelector('.notification-content').scrollHeight>el.querySelector('.notification-content').clientHeight};});
   assert(dimensions.left>=0&&dimensions.right<=width&&dimensions.bottom<=900);assert(dimensions.scrollable);
   if(width<=900) assert(dimensions.bottom<=await page.locator('.mobile-primary-nav').evaluate(el=>el.getBoundingClientRect().top),'Panel must clear bottom navigation');
   const targets=await panel.locator('button,a').evaluateAll(nodes=>nodes.map(n=>n.getBoundingClientRect().height));assert(targets.every(h=>h>=44));
   await page.screenshot({path:path.join(output,'notifications-'+width+'.png')});
   const accessibility=await new AxeBuilder({page}).include('[data-notification-panel]').withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
   assert.deepEqual(accessibility.violations.map(v=>v.id),[],'Notification panel accessibility');
   await page.keyboard.press('Tab');assert(await panel.getByRole('link',{name:'Rate title: '+items[0].title,exact:true}).evaluate(el=>el===document.activeElement));
   await page.keyboard.press('Escape');assert(await panel.isHidden());assert(await toggle.evaluate(el=>el===document.activeElement));
   await toggle.click();await page.locator('[data-notification-close]').click();assert(await toggle.evaluate(el=>el===document.activeElement));
   await toggle.click();await page.mouse.click(2,500);assert(await panel.isHidden());
   await toggle.click();assert.equal(reads,1);
   await page.route(base+'/plus/library/101',route=>route.fulfill({contentType:'text/html',body:'<!doctype html><title>Fixture title</title><h1>Existing title rating</h1>'}));
   await panel.getByRole('link',{name:'Rate title: '+items[0].title,exact:true}).click();
   await page.waitForURL(base+'/plus/library/101#watch-tracking');
   await page.getByRole('heading',{name:'Existing title rating'}).waitFor();
   for(let attempt=0;attempt<50&&!mutations.length;attempt++) await new Promise(resolve=>setTimeout(resolve,20));
   assert.equal(mutations[0].csrf,'notification-fixture-csrf');assert(mutations[0].path.includes(encodeURIComponent(items[0].id)));
   assert.equal(current[0].read,true,'Primary link marks read without blocking correct navigation');
   await page.goto(base+'/plus/home');
   await page.waitForFunction(()=>document.querySelector('[data-notification-badge]').textContent==='2');
   assert.equal(reads,2,'Only one idle GET on each actual page visit');await toggle.click();
   await panel.getByRole('button',{name:'Dismiss A Quiet Constellation',exact:true}).click();
   await page.waitForFunction(()=>document.querySelectorAll('.notification-item').length===4);
   assert.equal(await panel.getByText('A Quiet Constellation',{exact:true}).count(),0);assert.equal(reads,2);
   mutationStatus=403;
   await panel.getByRole('button',{name:'Dismiss The Last Evening',exact:true}).click();
   await page.waitForFunction(()=>!document.querySelector('[data-notification-feedback]').hidden);
   assert((await page.locator('[data-notification-feedback]').innerText()).includes('session'));
   assert.equal(await panel.getByText('The Last Evening',{exact:true}).count(),1);assert.equal(reads,2);
   const previousMutations=mutations.length;await page.locator('meta[name="csrf-token"]').evaluate(el=>el.content='');
   await panel.getByRole('button',{name:'Dismiss The Last Evening',exact:true}).click();
   assert.equal(mutations.length,previousMutations,'Missing CSRF must not send a mutation');
   assert.deepEqual(failures,[]);receipts.push({width,geometry,panel:dimensions,oneIdleGet:true,readDismissCsrf:true,unsafeHrefBlocked:true,testedPanelAxeViolations:0,keyboardAndOutside:true,mutationErrorPreserved:true});
   await context.close();
  }
  {
   const {context,page}=await setup(390);
   const toggle=page.locator('[data-notification-toggle]'),panel=page.locator('[data-notification-panel]');
   await page.waitForFunction(()=>!document.querySelector('[data-notification-badge]').hidden);await toggle.click();
   await page.route(base+'/plus/library/101',route=>route.fulfill({contentType:'text/html',body:'<!doctype html><title>Fixture</title><h1>Existing title rating</h1>'}));
   deferNextRead=true;const primary=panel.getByRole('link',{name:'Rate title: '+items[0].title,exact:true});
   await primary.evaluate(link=>link.target='_blank'); // Keep the source page alive for another user action.
   const popupReady=page.waitForEvent('popup');await primary.click();
   const popup=await popupReady;await popup.close();
   for(let attempt=0;attempt<50&&!releaseHeldResponse;attempt++) await new Promise(resolve=>setTimeout(resolve,20));
   assert(releaseHeldResponse,'Read acknowledgement response must be intentionally held');
   await panel.getByRole('button',{name:'Dismiss '+items[0].title,exact:true}).click();
   await new Promise(resolve=>setTimeout(resolve,100));
   assert.equal(mutations.length,1,'Dismiss request must wait for pending acknowledgement');
   releaseHeldResponse();releaseHeldResponse=null;
   await page.waitForFunction(()=>document.querySelectorAll('.notification-item').length===4);
   assert.equal(mutations.length,2);assert.equal(current.some(item=>item.id===items[0].id),false);
   await page.keyboard.press('Escape');await toggle.click();
   assert.equal(await panel.getByText(items[0].title,{exact:true}).count(),0,'Late acknowledgement must never restore a dismissed item');
   assert.equal(await page.locator('[data-notification-badge]').innerText(),'2');assert.equal(reads,1);
   receipts.push({scenario:'held-acknowledgement-then-dismiss',serializedRequests:true,noResurrection:true,correctUnread:true});await context.close();
  }
  for(const scenario of ['error','session','timeout']) {
   const {context,page}=await setup(390,scenario);
   await page.locator('[data-notification-toggle]').click();
   await page.getByText(scenario==='session'?'Sign in to view notifications':'Could not load notifications',{exact:true}).waitFor({timeout:8000});
   assert(await page.locator('[data-notification-badge]').isHidden());assert.equal(await page.locator('[data-notification-toggle]').getAttribute('aria-label'),'Notifications');assert.equal(reads,1);
   mode='normal';current=[];
   await page.getByRole('button',{name:'Try again',exact:true}).click();
   await page.getByText('You are all caught up',{exact:true}).waitFor();assert.equal(reads,2);
   receipts.push({scenario,unknownNeverZero:true,explicitRetryEmpty:true,abortDeadline:scenario==='timeout'});await context.close();
  }
  fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({method:'Synthetic actual base.html + full CSS, only new controller, mock local API; no accounts/providers',receipts},null,2));
  console.log(JSON.stringify({checks:receipts.length,widths:[1440,2560,390,320],states:['mixed','read','dismiss','session','error','timeout','empty'],result:'PASS'},null,2));
 } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);server.closeAllConnections();server.close();process.exit(1)});
