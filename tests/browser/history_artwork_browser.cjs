/* Actual home/activity templates and product CSS; disclosed synthetic still bytes. */
const fs=require('fs');
const path=require('path');
const http=require('http');
const assert=require('assert/strict');
const {chromium}=require(process.argv[4]||'playwright');
const fixture=process.argv[2],output=process.argv[3];
const staticRoot=path.resolve(__dirname,'../../src/omakase/plus/static');
const counts={};
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://local');counts[url.pathname]=(counts[url.pathname]||0)+1;
 if(url.pathname==='/plus/home'||url.pathname==='/plus/activity'){
  res.setHeader('content-type','text/html');res.end(fs.readFileSync(path.join(fixture,url.pathname.endsWith('home')?'home.html':'history.html')));return;
 }
 if(url.pathname==='/plus/api/notifications'){res.setHeader('content-type','application/json');res.end('{"items":[],"count":0,"unread_count":0}');return;}
 const images={'/plus/history/artwork/anime/1':'episode1.png','/plus/history/artwork/anime/2':'episode2.png','/plus/history/artwork/tv/42/1/2':'episode2.png','/plus/tv/42/artwork':'series.png','/plus/media/21/poster':'series.png','/plus/movies/tt0084787/artwork':'movie.png'};
 if(images[url.pathname]){res.setHeader('content-type','image/png');res.end(fs.readFileSync(path.join(fixture,images[url.pathname])));return;}
 if(url.pathname.startsWith('/plus/static/')){
  const name=url.pathname.slice('/plus/static/'.length);
  if(!/^[\w.-]+$/.test(name)){res.writeHead(404);res.end();return;}
  const file=path.join(staticRoot,name);
  if(fs.existsSync(file)&&fs.statSync(file).isFile()){
   res.setHeader('content-type',name.endsWith('.css')?'text/css':name.endsWith('.js')?'application/javascript':name.endsWith('.woff2')?'font/woff2':name.endsWith('.svg')?'image/svg+xml':'image/png');res.end(fs.readFileSync(file));return;
  }
 }
 res.writeHead(404);res.end();
});
(async()=>{
 await new Promise(r=>server.listen(process.argv.includes('--serve-only')?4436:0,'127.0.0.1',r));
 const port=server.address().port;
 if(process.argv.includes('--serve-only')){console.log(JSON.stringify({url:`http://127.0.0.1:${port}/plus/home`,pid:process.pid,synthetic:true,noOutbound:true}));return;}
 fs.mkdirSync(output,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const receipt=[];
 try{
 for(const width of [320,390,1440])for(const surface of ['home','history']){
  const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
  const page=await context.newPage();let external=0;
  await page.route('**/*',route=>{if(new URL(route.request().url()).hostname!=='127.0.0.1'){external++;return route.abort();}return route.continue();});
  await page.goto(`http://127.0.0.1:${port}/plus/${surface==='home'?'home':'activity?view=watch'}`);
  const cards=page.locator('.history-card');assert.equal(await cards.count(),6);
  for(let i=0;i<5;i++){
   const image=cards.nth(i).locator('img');await cards.nth(i).scrollIntoViewIfNeeded();
   const missing=i===2||i===4;
   if(missing){
    await page.waitForFunction(el=>el.dataset.artworkScope==='unavailable',await image.elementHandle());
    assert(await image.isHidden());
    assert(await cards.nth(i).locator('[data-history-placeholder]').isVisible());
    assert((await cards.nth(i).locator('[data-history-placeholder]').getAttribute('aria-label')).includes('image unavailable'));
   }else{
    await image.evaluate(el=>new Promise((resolve,reject)=>{const check=()=>el.complete&&el.naturalWidth?resolve():setTimeout(check,40);check();setTimeout(()=>reject(Error('Image did not load')),4000);}));
    assert.equal(await image.getAttribute('data-artwork-scope'),'episode');
    assert(await cards.nth(i).locator('[data-history-placeholder]').isHidden());
    const pixel=await image.evaluate(el=>{const c=document.createElement('canvas');c.width=1;c.height=1;c.getContext('2d').drawImage(el,0,0,1,1);return Array.from(c.getContext('2d').getImageData(0,0,1,1).data);});
    assert(pixel[i===0?0:2]>100,'Distinct exact episode pixels must be rendered');
   }
   assert.equal(await image.getAttribute('loading'),'lazy');
  }
  assert.equal(counts['/plus/media/21/poster']||0,0,'Missing episode must not request series poster');
  assert.equal(counts['/plus/tv/42/artwork']||0,0,'Missing TV episode must not request series poster');
  assert.equal(await cards.nth(5).locator('img').getAttribute('src'),'/plus/movies/tt0084787/artwork');
  assert((await cards.nth(0).innerText()).includes('Observed'));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  assert.equal(external,0);
  await cards.nth(0).scrollIntoViewIfNeeded();await cards.nth(0).locator('a').focus();assert(await cards.nth(0).locator('a').evaluate(el=>el===document.activeElement));
  await page.evaluate(()=>{document.activeElement?.blur();window.scrollTo(0,0);});
  await page.waitForTimeout(150);
  await page.screenshot({path:path.join(output,`${surface}-${width}.png`),fullPage:true});
  receipt.push({surface,width,actualTemplate:true,exactPixelsDistinct:true,deliberateEpisodePlaceholder:true,movieUnchanged:true,noOverflow:true,noExternalRequests:true});await context.close();
 }
 fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({method:'Actual home/activity/base templates + actual CSS/JS; synthetic local episode images only; unavailable exact stills use episode placeholders',cases:receipt},null,2));
 console.log(JSON.stringify({result:'PASS',cases:receipt.length,widths:[320,390,1440]}));
 }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(error=>{console.error(error);server.closeAllConnections();server.close();process.exit(1);});
