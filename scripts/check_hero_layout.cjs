const {qaRuntime, qaOutput, qaBrowser} = require('./qa-environment.cjs');
/* Safe preview hero regression. Intercepted art is synthetic; no providers or writes. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const runtime = qaRuntime();
const {chromium} = require(require.resolve('playwright', {paths:[runtime]}));
const base = process.env.OMAKASE_REVIEW_URL || 'http://127.0.0.1:4418';
assert.equal(new URL(base).hostname,'127.0.0.1','only the safe loopback preview');
const selectedWidths=(process.argv.find(value=>value.startsWith('--width='))?.slice(8)||'').split(',').filter(Boolean).map(Number);
const selectedArts=(process.argv.find(value=>value.startsWith('--art='))?.slice(6)||'').split(',').filter(Boolean);
const realArtFiles=(process.argv.find(value=>value.startsWith('--art-files='))?.slice(12)||'').split(',').filter(Boolean).map(file=>({name:path.basename(file,path.extname(file)),path:path.resolve(file),bytes:fs.readFileSync(file)}));
const landscapePath=process.argv.find(value=>value.startsWith('--landscape-file='))?.slice(17);
const landscapeFile=landscapePath?{path:path.resolve(landscapePath),bytes:fs.readFileSync(landscapePath)}:null;
const output = process.env.OMAKASE_HERO_QA_OUTPUT || qaOutput('hero_layout');
fs.mkdirSync(output,{recursive:true});
const root=path.resolve(__dirname,'..');
const results={url:base+'/plus/home',sourceSha256:{},scenarios:[],limitations:['Synthetic intercepted artwork proves layout and source sizing, not creative quality or physical-device acceptance.']};
if(realArtFiles.length){results.artwork=realArtFiles.map(file=>({name:file.name,path:file.path,bytes:file.bytes.length,sha256:crypto.createHash('sha256').update(file.bytes).digest('hex')}));results.limitations=['Actual cached MARRIAGETOXIN and contrasting poster files are finite artwork fixtures on the actual sample-account Home template. Contrasting files replace the featured artwork only; they do not test catalog identity or playback. Physical-device acceptance is separate.'];}
if(landscapeFile){results.artwork=[{...landscapeFile,bytes:landscapeFile.bytes.length,sha256:crypto.createHash('sha256').update(landscapeFile.bytes).digest('hex')}];results.limitations=['Genuine cached landscape artwork replaces only the featured art on the actual sample-account Home template. This verifies artwork composition, not catalog identity, playback or a physical device.'];}
for(const file of ['src/omakase/plus/templates/home.html','src/omakase/plus/static/hero-quality.js','src/omakase/plus/static/hero-quality.css']) results.sourceSha256[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex');
const svg=(w,h,empty=false)=>`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">${empty?'':`<rect width="100%" height="100%" fill="#436883"/><circle cx="${w*.67}" cy="${h*.42}" r="${Math.min(w,h)*.16}" fill="#d8a67b"/>`}</svg>`;

async function measure(page){return page.evaluate(()=>{
 const picture=document.querySelector('picture[data-hero-quality]');const img=picture.querySelector('img');const s=getComputedStyle(img);const box=img.getBoundingClientRect();
 const atmosphere=document.querySelector('.cinema-hero-atmosphere');const atmosphereImage=atmosphere.querySelector('img');const a=getComputedStyle(atmosphere);
 const title=document.querySelector('#featured-title');const actions=[...document.querySelectorAll('.cinema-actions a')];const hero=document.querySelector('.cinema-hero');const copy=document.querySelector('.cinema-hero-copy');
 const scale=img.naturalWidth&&img.naturalHeight?(s.objectFit==='cover'?Math.max(box.width/img.naturalWidth,box.height/img.naturalHeight):Math.min(box.width/img.naturalWidth,box.height/img.naturalHeight)):null;
 return {state:picture.dataset.heroState,fit:s.objectFit,visibility:s.visibility,natural:[img.naturalWidth,img.naturalHeight],box:box.toJSON(),drawnScale:scale,atmosphere:{filter:atmosphereImage?getComputedStyle(atmosphereImage).filter:a.filter,background:a.backgroundImage,images:atmosphere.querySelectorAll('img').length,box:atmosphere.getBoundingClientRect().toJSON()},title:{text:title.textContent,box:title.getBoundingClientRect().toJSON()},actions:actions.map(n=>({name:n.textContent.trim(),box:n.getBoundingClientRect().toJSON()})),hero:hero.getBoundingClientRect().toJSON(),copy:copy.getBoundingClientRect().toJSON(),shelf:document.querySelector('#continue-heading').getBoundingClientRect().toJSON(),overflow:document.documentElement.scrollWidth-innerWidth,viewport:{width:innerWidth,height:innerHeight},animation:getComputedStyle(copy).animationName};
 });}
function verify(facts,scenario,fail){
 fail(facts.overflow<=1,'page has no horizontal overflow');
 fail(facts.title.box.width>0&&facts.title.box.height>0,'title is visible');
 fail(facts.actions.length>0&&facts.actions.length<=2,'one or two hero actions');
 for(const action of facts.actions){
  fail(action.box.height>=44,'touch-sized hero action');
  fail(action.box.top>=facts.title.box.bottom,'title and actions do not overlap');
  fail(action.box.left>=-1&&action.box.right<=facts.viewport.width+1,'hero action fits page width');
  fail(action.box.bottom<=facts.hero.bottom+1,'hero contains its action');
  fail(facts.shelf.top>=action.box.bottom,'first shelf does not collide with actions');
  if(scenario.width<=390)fail(action.box.bottom<=facts.viewport.height-60,'phone hero actions visible above bottom navigation');
 }
 fail(facts.animation==='none','reduced-motion copy is composed on first paint');
 if(scenario.art==='unavailable'){
  fail(facts.state==='unavailable'&&facts.visibility==='hidden','missing poster and banner have an explicit unavailable state');
 }else{
  fail(facts.state=== (scenario.art==='landscape'?'cover':'contained'),'expected cover or composed fallback');
  fail(facts.drawnScale!==null&&facts.drawnScale<=1.25,'sharp image enlarges by no more than 1.25');
  if(scenario.art!=='landscape'){fail(facts.atmosphere.images===0&&facts.atmosphere.background.includes('gradient'),'contained artwork has a clean gradient without duplicate blurred art');fail(Math.abs(facts.box.width/facts.box.height-facts.natural[0]/facts.natural[1])<.01,'visible poster frame matches actual image aspect without a letterbox matte');}
 }
}
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 try{
  const scenarios=[];
  if(landscapeFile){for(const width of [1440,390,320])scenarios.push({width,art:'landscape',fixture:'cached-landscape'});}
  else if(realArtFiles.length){for(const width of [1440,390,320])for(const file of realArtFiles)scenarios.push({width,art:'portrait',fixture:file.name});}
  else {for(const width of [1440,2560,390,320])for(const art of ['landscape','portrait','tiny','missing'])scenarios.push({width,art});for(const width of [1440,320])scenarios.push({width,art:'unavailable'});}
  for(const scenario of scenarios.filter(value=>(!selectedArts.length||selectedArts.includes(value.art))&&(!selectedWidths.length||selectedWidths.includes(value.width)))){
   const context=await browser.newContext({viewport:{width:scenario.width,height:scenario.width<=390?844:1000},reducedMotion:'reduce'});
   const page=await context.newPage();const failures=[];const errors=[];const requests=[];
   const fail=(pass,message)=>{if(!pass)failures.push(message);};
   page.on('pageerror',e=>errors.push(e.message));
   await page.addInitScript(()=>{window.__heroStates=[];new MutationObserver(records=>records.forEach(r=>{if(r.target.matches?.('picture[data-hero-quality]')&&r.attributeName==='data-hero-state')window.__heroStates.push({state:r.target.dataset.heroState,at:performance.now()});})).observe(document,{subtree:true,attributes:true,attributeFilter:['data-hero-state']});});
   await page.route('**/*',async route=>{
    const request=route.request();const url=new URL(request.url());requests.push(url.pathname);
    if(request.method()!=='GET'||url.origin!==new URL(base).origin)return route.abort('blockedbyclient');
    if(scenario.fixture){
     if(landscapeFile&&url.pathname==='/plus/media/199547/banner')return route.fulfill({contentType:'image/jpeg',body:landscapeFile.bytes});
     if(url.pathname==='/plus/media/199547/banner')return route.fulfill({status:404,body:''});
     if(!landscapeFile&&url.pathname==='/plus/media/199547/poster')return route.fulfill({contentType:'image/jpeg',body:realArtFiles.find(file=>file.name===scenario.fixture).bytes});
     return route.continue();
    }
    if(/\/banner$/.test(url.pathname)){
     if(['missing','unavailable'].includes(scenario.art))return route.fulfill({status:404,body:''});
     const size=scenario.art==='landscape'?[3307,1860]:scenario.art==='portrait'?[480,720]:[16,9];
     return route.fulfill({contentType:'image/svg+xml',body:svg(...size)});
    }
    if(/\/poster$/.test(url.pathname)){
     if(scenario.art==='unavailable')return route.fulfill({status:404,body:''});
     return route.fulfill({contentType:'image/svg+xml',body:svg(480,720)});
    }
    if(url.pathname==='/plus/home'||url.pathname==='/plus/account/avatar'||url.pathname.startsWith('/plus/static/'))return route.continue();
    return route.abort('blockedbyclient');
   });
   try{
    await page.goto(base+'/plus/home',{waitUntil:'networkidle'});
    await page.waitForFunction(()=>['cover','contained','unavailable'].includes(document.querySelector('picture[data-hero-quality]')?.dataset.heroState),null,{timeout:5000});
    await page.waitForTimeout(150);
    const initial=await measure(page);verify(initial,scenario,fail);
    if(scenario.fixture)fail(initial.title.text==='MARRIAGETOXIN','cached-art fixture targets the actual problematic featured title');
    await page.screenshot({path:path.join(output,`${scenario.fixture||scenario.art}-${scenario.width}.png`),fullPage:false});
    const states=[];
    for(const width of [scenario.width<=390?1440:390,scenario.width]){
     await page.setViewportSize({width,height:width<=390?844:1000});
     await page.waitForTimeout(200);
     const first=await measure(page);await page.waitForTimeout(200);const second=await measure(page);
     fail(first.state===second.state,'hero state settles after resize');
     fail(Math.abs(first.box.width-second.box.width)<1&&Math.abs(first.box.height-second.box.height)<1,'hero geometry does not oscillate after resize');
     states.push({width,first,second});
    }
    const changes=await page.evaluate(()=>window.__heroStates);
    fail(changes.length<=12,'bounded hero state transitions');
    fail(errors.length===0,'no JavaScript errors');
    results.scenarios.push({...scenario,initial,resizes:states,stateChanges:changes,failures,errors,requests});
   }catch(error){await page.screenshot({path:path.join(output,`${scenario.fixture||scenario.art}-${scenario.width}-failed.png`),fullPage:false}).catch(()=>{});results.scenarios.push({...scenario,failures:[...failures,error.message],errors,requests});}
   await context.close();
  }
 }finally{await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(results,null,2));}
 const failed=results.scenarios.filter(s=>s.failures.length);
 console.log(JSON.stringify({scenarios:results.scenarios.length,passed:results.scenarios.length-failed.length,failed:failed.map(s=>({art:s.art,width:s.width,failures:s.failures})),output},null,2));
 if(failed.length)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
