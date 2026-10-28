const {qaRuntime, qaPython, qaBrowser} = require('./qa-environment.cjs');
/* Isolated actual-template browser regression: no providers, servers or writes. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const runtime = qaRuntime();
const {chromium} = require(require.resolve('playwright', {paths:[runtime]}));
const root = path.resolve(__dirname, '..');
const html = execFileSync(qaPython(), ['-c', "from scripts.preview_plus_workspace import render; print(render('home'))"], {cwd:root, encoding:'utf8', env:{...process.env,PYTHONIOENCODING:'utf-8'}});
const staticRoot = path.join(root,'src/omakase/plus/static');
const svg = (width,height,transparent=false) => `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">${transparent?'':`<rect width="100%" height="100%" fill="#467898"/>`}</svg>`;
const results=[];
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 try {
  for(const scenario of [
   {name:'wide-landscape',width:1440,art:[2560,1440],state:'cover'},
   {name:'large-desktop-landscape',width:2560,art:[2560,1440],state:'cover'},
   {name:'low-resolution-banner',width:1440,art:[900,300],state:'contained'},
   {name:'portrait-banner',width:1440,art:[480,720],state:'contained'},
   {name:'transparent-placeholder',width:1440,art:[2,3],transparent:true,state:'unavailable'},
   {name:'mobile-poster',width:390,art:[2560,1440],state:'contained',dpr:2},
   {name:'no-javascript',width:1440,art:[900,300],noJS:true},
  ]) {
   const context=await browser.newContext({viewport:{width:scenario.width,height:900},deviceScaleFactor:scenario.dpr||1,javaScriptEnabled:!scenario.noJS,reducedMotion:'reduce'});
   const page=await context.newPage();
   const errors=[];page.on('pageerror',error=>errors.push(error.message));
   const seen=[];
   await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/plus/home') return route.fulfill({contentType:'text/html',body:html});
    if(url.pathname.startsWith('/plus/static/')) {
     const file=path.join(staticRoot,path.basename(url.pathname));
     if(fs.existsSync(file))return route.fulfill({path:file});
    }
    if(url.pathname.match(/\/(banner|poster)$/)) {
     seen.push(url.pathname);
     const size=url.pathname.endsWith('/banner')?scenario.art:scenario.transparent?[2,3]:[480,720];
     return route.fulfill({contentType:'image/svg+xml',body:svg(...size,scenario.transparent)});
    }
    return route.fulfill({status:404,body:''});
   });
   await page.goto('http://hero.test/plus/home',{waitUntil:'networkidle'});
   const picture=page.locator('picture[data-hero-quality]');
   if(scenario.state)await picture.waitFor({state:'visible'});
   if(scenario.state)await page.waitForFunction(state=>document.querySelector('picture[data-hero-quality]').dataset.heroState===state,scenario.state);
   const proof=await picture.evaluate(node=>{
    const image=node.querySelector('img');const style=getComputedStyle(image);const bounds=image.getBoundingClientRect();
    return {state:node.dataset.heroState,fit:style.objectFit,natural:[image.naturalWidth,image.naturalHeight],box:[bounds.width,bounds.height],visibility:style.visibility};
   });
   if(scenario.state)assert.equal(proof.state,scenario.state,scenario.name);
   if(proof.state!=='cover')assert.equal(proof.fit,'scale-down',scenario.name);
   if(proof.state==='contained')assert(proof.box[0]<=proof.natural[0]&&proof.box[1]<=proof.natural[1],scenario.name+' must not enlarge source');
   if(scenario.transparent)assert.equal(proof.visibility,'hidden');
   assert.equal(await page.locator('#featured-title').isVisible(),true);
   assert.deepEqual(errors,[]);
   const heroPoster=await picture.getAttribute('data-hero-poster');
   assert(seen.filter(value=>value===heroPoster).length<=1,'poster fallback must load at most once');
   if(scenario.name==='low-resolution-banner') {
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.querySelector('picture[data-hero-quality]').dataset.heroState==='contained');
    assert.equal(await picture.locator('img').evaluate(image=>getComputedStyle(image).objectFit),'scale-down');
   }
   results.push({name:scenario.name,...proof,errors});
   await context.close();
  }
  console.log(JSON.stringify({passed:true,results},null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
