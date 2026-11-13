/* Actual read-only sample-account routes. Measures explanatory text before actions. */
const {qaRuntime, qaOutput, qaBrowser}=require('./qa-environment.cjs');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {execFileSync}=require('node:child_process');
const {chromium}=require(require.resolve('playwright',{paths:[qaRuntime()]}));
const root=path.resolve(__dirname,'..');
const base=process.env.OMAKASE_REVIEW_URL||'http://127.0.0.1:4418';
if(new URL(base).hostname!=='127.0.0.1')throw Error('Read-only loopback preview required');
const output=process.env.OMAKASE_SPACING_QA_OUTPUT||qaOutput('text_action_spacing');
const baseline=process.argv.includes('--baseline');
const audit=process.argv.includes('--audit');
const selected=(process.argv.find(value=>value.startsWith('--case='))?.slice(7)||'').split(',').filter(Boolean);
const oldCss=baseline?execFileSync('git',['show','HEAD:src/omakase/plus/static/refinement.css'],{cwd:root}):null;
fs.mkdirSync(output,{recursive:true});
const result={baseline,url:base,sourceSha256:{},cases:[],limitations:['Sample account and GET-only browser verification; no household, settings or acquisition action was submitted. Enlarged text is a browser text-size check, not a physical-device result.']};
for(const name of ['household.html','_settings_storage.html','library.html']){
 const file='src/omakase/plus/templates/'+name;result.sourceSha256[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex');
}
result.sourceSha256['src/omakase/plus/static/refinement.css']=crypto.createHash('sha256').update(oldCss||fs.readFileSync(path.join(root,'src/omakase/plus/static/refinement.css'))).digest('hex');
const routes=[
 {name:'household',url:'/plus/settings/household',target:'.household-panel>p+.household-actions'},
 {name:'join',url:'/plus/households/join',target:'.household-panel>p+form'},
 {name:'storage',url:'/plus/settings/storage',target:'.storage-choice>p+.button'},
 {name:'empty-library',url:'/plus/library?query=spacing-fixture-no-matching-title',target:'.product-empty>p+.button'},
];
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 try{
  if(audit){
   const nav=fs.readFileSync(path.join(root,'src/omakase/plus/templates/_settings_nav.html'),'utf8');
   const urls=[...new Set([...nav.matchAll(/settings_link\('[^']+',\s*'([^']+)'/g)].map(m=>m[1]).concat(['/plus/settings/household/create','/plus/households/join','/plus/demo/states/title/populated','/plus/library?query=spacing-fixture-no-matching-title']))];
   for(const url of urls){
    const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'}),page=await context.newPage(),writes=[],errors=[],failures=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',route=>{const req=route.request();if(req.method()!=='GET'){writes.push(req.url());return route.abort('blockedbyclient');}if(new URL(req.url()).origin!==new URL(base).origin)return route.abort('blockedbyclient');return route.continue();});
    let pairs=[];
    try{
     const response=await page.goto(base+url,{waitUntil:'networkidle'});if(response.status()!==200)failures.push('Route status '+response.status());
     pairs=await page.evaluate(()=>[...document.querySelectorAll('main p')].flatMap(p=>{
      const action=p.nextElementSibling;if(!action||!action.matches('.button,form,[class*=actions],.settings-submit,.button-row'))return [];
      const pb=p.getBoundingClientRect(),box=action.getBoundingClientRect();if(!pb.height||!box.height||box.right<=pb.left||box.left>=pb.right)return [];
      const range=document.createRange();range.selectNodeContents(p);const last=[...range.getClientRects()].sort((a,b)=>b.bottom-a.bottom)[0]||pb;
      return [{text:p.textContent.trim(),parent:p.parentElement.className,action:action.className||action.tagName,gap:box.top-pb.bottom,lineGap:box.top-last.bottom,crowded:box.top-last.bottom<12}];
     }));
     if(pairs.some(p=>p.crowded)){failures.push('Explanatory text crowds a separate action');await page.screenshot({path:path.join(output,`audit-${result.cases.length}.png`),fullPage:true});}
     if(errors.length||writes.length)failures.push('Unexpected script error or write');
    }catch(error){failures.push(error.message);}
    result.cases.push({url,width:390,pairs,failures,errors,writes});await context.close();
   }
  }else for(const width of [1440,390,320])for(const scale of [1,2])for(const scenario of routes.filter(row=>!selected.length||selected.includes(row.name))){
   const context=await browser.newContext({viewport:{width,height:width<500?844:1000},reducedMotion:'reduce'});
   const page=await context.newPage(),errors=[],writes=[],failures=[];
   const check=(condition,message)=>{if(!condition)failures.push(message);};
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',route=>{
    const req=route.request(),url=new URL(req.url());
    if(req.method()!=='GET'){writes.push(url.pathname);return route.abort('blockedbyclient');}
    if(url.origin!==new URL(base).origin)return route.abort('blockedbyclient');
    if(oldCss&&url.pathname==='/plus/static/refinement.css')return route.fulfill({contentType:'text/css',body:oldCss});
    return route.continue();
   });
   let facts;
   try{
    await page.goto(base+scenario.url,{waitUntil:'networkidle'});
    if(scale===2)await page.evaluate(()=>{
     // Scale computed text once, retaining the route's real responsive containers.
     for(const node of document.querySelectorAll('main p,main h2,main label,main .button,main summary,main small')){
      const style=getComputedStyle(node);node.dataset.qaFont=style.fontSize;
     }
     for(const node of document.querySelectorAll('[data-qa-font]'))node.style.fontSize=(parseFloat(node.dataset.qaFont)*2)+'px';
    });
    const target=page.locator(scenario.target).first();await target.waitFor({state:'visible',timeout:5000});
    await target.scrollIntoViewIfNeeded();
    facts=await target.evaluate(node=>{
     const p=node.previousElementSibling,range=document.createRange();range.selectNodeContents(p);
     const lines=[...range.getClientRects()].map(r=>r.toJSON());
     const last=lines.sort((a,b)=>b.bottom-a.bottom)[0]||p.getBoundingClientRect().toJSON();
     const box=node.getBoundingClientRect();
     const controls=node.matches('.button')?[node]:[...node.querySelectorAll('a.button,button,input:not([type=hidden]),select')];
     return {paragraph:p.textContent.trim(),paragraphBox:p.getBoundingClientRect().toJSON(),lastLine:last,action:box.toJSON(),gap:box.top-p.getBoundingClientRect().bottom,lineGap:box.top-last.bottom,controls:controls.map(n=>({text:n.textContent.trim(),box:n.getBoundingClientRect().toJSON()})),overflow:document.documentElement.scrollWidth-innerWidth};
    });
    check(facts.gap>=19,'explanation has at least20px before separate action block');
    check(facts.lineGap>=19,'last wrapped text line stays separate from action block');
    check(facts.controls.length>0,'action controls exist');
    for(const control of facts.controls){check(control.box.height>=44,'action has44px target');check(control.box.left>=-1&&control.box.right<=width+1,'wrapped action stays within viewport');}
    check(facts.overflow<=1,'page has no horizontal overflow');
    check(!errors.length,'no JavaScript errors');check(!writes.length,'no action submitted');
    await page.screenshot({path:path.join(output,`${scenario.name}-${width}-text${scale}.png`),fullPage:false});
   }catch(error){failures.push(error.message);}
   result.cases.push({...scenario,width,textScale:scale,facts,failures,errors,writes});
   await context.close();
  }
 }finally{await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(result,null,2));}
 const failed=result.cases.filter(c=>c.failures.length);
 console.log(JSON.stringify({cases:result.cases.length,passed:result.cases.length-failed.length,failed:failed.map(c=>({name:c.name,width:c.width,textScale:c.textScale,failures:c.failures})),output},null,2));
 if(failed.length)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
