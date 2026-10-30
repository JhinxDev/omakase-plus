/* Spatial palette regression on the actual Home template; no external requests/writes. */
const {qaRuntime, qaOutput, qaBrowser} = require('./qa-environment.cjs');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {chromium} = require(require.resolve('playwright', {paths:[qaRuntime()]}));
const base = process.env.OMAKASE_REVIEW_URL || 'http://127.0.0.1:4419';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = process.env.OMAKASE_EDGE_QA_OUTPUT || qaOutput('hero_edge_palette');
fs.mkdirSync(output, {recursive:true});
const artwork = (process.argv.find(value=>value.startsWith('--art-files='))?.slice(12)||'').split(',').filter(Boolean).map(file=>({name:path.basename(file,path.extname(file)),bytes:fs.readFileSync(file)}));
const colors = {tl:[240,150,48],tr:[36,174,112],bl:[240,92,136],br:[38,112,232]};
const svg = (swap=false)=>`<svg xmlns="http://www.w3.org/2000/svg" width="480" height="720"><path fill="rgb(${colors.tl})" d="M0 0h240v360H0z"/><path fill="rgb(${colors.tr})" d="M240 0h240v360H240z"/><path fill="rgb(${swap?colors.br:colors.bl})" d="M0 360h240v360H0z"/><path fill="rgb(${swap?colors.bl:colors.br})" d="M240 360h240v360H240z"/><path fill="#11121a" d="M120 120h240v480H120z"/></svg>`;
const receipt={url:base+'/plus/home',source:{},scenarios:[],limitations:['Finite synthetic color zones and cached artwork verify local rendering and image-change behavior. No live account or physical device was used.']};
for(const file of ['src/omakase/plus/static/hero-quality.js','src/omakase/plus/static/hero-quality.css']) receipt.source[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,'..',file))).digest('hex');
async function facts(page){return page.evaluate(()=>{
 const hero=document.querySelector('.cinema-hero'),picture=hero.querySelector('[data-hero-quality]'),img=picture.querySelector('img');
 const style=getComputedStyle(hero),bounds=hero.getBoundingClientRect(),art=img.getBoundingClientRect();
 return {palette:hero.dataset.heroPalette,state:picture.dataset.heroState,colors:Object.fromEntries(['tl','top','tr','left','right','bl','bottom','br'].map(key=>[key,style.getPropertyValue('--hero-edge-'+key).trim().split(',').map(Number)])),anchors:Object.fromEntries(['xleft','xcenter','xright','ytop','ycenter','ybottom'].map(key=>[key,parseFloat(style.getPropertyValue('--hero-edge-'+key))])),art:art.toJSON(),hero:bounds.toJSON(),overflow:document.documentElement.scrollWidth-innerWidth,images:hero.querySelector('.cinema-hero-atmosphere').querySelectorAll('img').length};
});}
function verify(value){
 assert.equal(value.palette,'perimeter-v1');assert.equal(value.state,'contained');assert.equal(value.images,0);assert.ok(value.overflow<=1);
 for(const rgb of Object.values(value.colors))assert.ok(rgb.length===3&&rgb.every(channel=>Number.isFinite(channel)&&channel>=0&&channel<=255));
 assert.ok(Math.abs(value.anchors.xleft-(value.art.left-value.hero.left)/value.hero.width*100)<.1);
 assert.ok(Math.abs(value.anchors.ybottom-(value.art.bottom-value.hero.top)/value.hero.height*100)<.1);
}
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 try{
  for(const width of [1440,390,320])for(const fixture of [{name:'spatial-zones'},...artwork]){
   const context=await browser.newContext({viewport:{width,height:width<=390?844:1000},reducedMotion:'reduce'}),page=await context.newPage(),errors=[];
   page.on('pageerror',error=>errors.push(error.message));
   await page.route('**/*',route=>{
    const request=route.request(),url=new URL(request.url());
    if(request.method()!=='GET'||url.hostname!=='127.0.0.1')return route.abort('blockedbyclient');
    if(url.pathname==='/plus/media/199547/banner')return route.fulfill({status:404,body:''});
    if(url.pathname==='/plus/media/199547/poster')return route.fulfill({contentType:fixture.bytes&&!url.searchParams.has('spatial-swap')?'image/jpeg':'image/svg+xml',body:fixture.bytes&&!url.searchParams.has('spatial-swap')?fixture.bytes:svg(url.searchParams.has('spatial-swap'))});
    return route.continue();
   });
   const result={width,fixture:fixture.name,failures:[]};
   try{
    await page.goto(base+'/plus/home',{waitUntil:'networkidle'});
    await page.waitForFunction(()=>document.querySelector('.cinema-hero')?.dataset.heroPalette==='perimeter-v1');
    result.initial=await facts(page);verify(result.initial);
    if(!fixture.bytes)for(const key of ['tl','tr','bl','br'])assert.ok(Math.hypot(...result.initial.colors[key].map((value,index)=>value-colors[key][index]))<12,`${key} retains its own edge color`);
    await page.screenshot({path:path.join(output,`${fixture.name}-${width}.png`),fullPage:false});
    // Change the image on the same mounted hero and verify bottom colors exchange places.
    await page.evaluate(()=>{document.querySelector('[data-hero-quality] img').src='/plus/media/199547/poster?spatial-swap=1';});
    await page.waitForFunction(()=>getComputedStyle(document.querySelector('.cinema-hero')).getPropertyValue('--hero-edge-bl').trim().startsWith('38,'));
    result.swapped=await facts(page);verify(result.swapped);
    assert.ok(Math.hypot(...result.swapped.colors.bl.map((value,index)=>value-colors.br[index]))<12);
    assert.ok(Math.hypot(...result.swapped.colors.br.map((value,index)=>value-colors.bl[index]))<12);
    await page.route('**/palette-unavailable',route=>route.fulfill({status:404,body:''}));
    await page.evaluate(()=>{const picture=document.querySelector('[data-hero-quality]');picture.dataset.heroPoster='';picture.querySelector('img').src='/palette-unavailable';});
    await page.waitForFunction(()=>document.querySelector('[data-hero-quality]').dataset.heroState==='unavailable');
    result.failed=await facts(page);assert.equal(result.failed.palette,'fallback');
    assert.ok(Object.values(result.failed.colors).every(rgb=>rgb.length===1&&rgb[0]===0),'previous edge colors cleared on failure');
    assert.deepEqual(errors,[]);
   }catch(error){result.failures.push(error.message);}
   receipt.scenarios.push(result);await context.close();
  }
 }finally{await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2));}
 const failures=receipt.scenarios.filter(result=>result.failures.length);process.stdout.write(JSON.stringify({scenarios:receipt.scenarios.length,failed:failures,receipt:path.join(output,'receipt.json')},null,2)+'\n');if(failures.length)process.exitCode=1;
})();
