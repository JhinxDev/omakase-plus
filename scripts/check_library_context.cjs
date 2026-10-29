/* Actual shared shell, synthetic scope DTOs, intercepted requests and native controls. */
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const {qaRuntime,qaOutput,qaPython,qaBrowser}=require('./qa-environment.cjs');
const {chromium}=require(require.resolve('playwright',{paths:[qaRuntime()]}));
const root=path.resolve(__dirname,'..');
const output=process.env.OMAKASE_LIBRARY_QA_OUTPUT||qaOutput('library_context');
assert(!fs.existsSync(output),'new output directory required');fs.mkdirSync(output,{recursive:true});
const source=`import json,runpy
from types import SimpleNamespace
suite=runpy.run_path('tests/plus/test_streaming_templates.py')
personal={'id':'sample-personal','name':'Personal library','kind':'personal'}
shared={'id':'sample-shared','name':'Morgan household with a deliberately long shared library name for phone containment','kind':'household'}
result={}
for state in ('personal','multiple','shared','unavailable'):
    selected=shared if state=='shared' else personal
    navigation={'available':state!='unavailable','selected_id':selected['id'],'selected_kind':selected['kind'],'libraries':[personal]+([shared] if state in ('multiple','shared') else [])}
    result[state]=suite['render']('base.html',request=SimpleNamespace(url=SimpleNamespace(path='/plus/home')),households_enabled=True,household_navigation=navigation,display_name='Alex Morgan')
print(json.dumps(result))`;
const pages=JSON.parse(execFileSync(qaPython(),['-c',source],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
const staticRoot=path.join(root,'src/omakase/plus/static');
const receipt={status:'RUNNING',at:new Date().toISOString(),sourceSha256:{},checks:[]};
for(const file of ['templates/base.html','templates/_library_context.html','static/household.css','static/dashboard.js'])receipt.sourceSha256[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,'src/omakase/plus',file))).digest('hex');
(async()=>{
  const browser=await chromium.launch(qaBrowser());
  try{
    for(const width of [1440,390,320])for(const state of Object.keys(pages)){
      const context=await browser.newContext({viewport:{width,height:844},reducedMotion:'reduce'});
      const page=await context.newPage();const errors=[];const posts=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',route=>{
        const req=route.request();const url=new URL(req.url());
        if(req.method()==='POST'){
          posts.push({path:url.pathname,data:Object.fromEntries(new URLSearchParams(req.postData()))});
          return route.fulfill({status:405,body:'Synthetic form capture; no action executed.'});
        }
        if(url.pathname==='/plus/home')return route.fulfill({contentType:'text/html',body:pages[state]});
        if(url.pathname.startsWith('/plus/static/')){
          const file=path.resolve(staticRoot,url.pathname.slice('/plus/static/'.length));
          if(file.startsWith(staticRoot+path.sep)&&fs.existsSync(file))return route.fulfill({path:file});
        }
        return route.fulfill({status:404,body:''});
      });
      await page.goto('http://library.test/plus/home',{waitUntil:'networkidle'});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'page contained');
      if(state==='personal'){
        assert.equal(await page.locator('.library-context').count(),0,'no redundant personal switch');
        await page.locator('[data-account-menu]>summary').click();
        assert(await page.getByRole('link',{name:'Personal library',exact:true}).isVisible(),'account scope available');
        await page.keyboard.press('Escape');
      }else if(state==='unavailable'){
        assert(await page.getByRole('status').filter({hasText:'Library selection is temporarily unavailable'}).isVisible(),'honest unavailable state');
        assert.equal(await page.locator('select[name=library_id]').count(),0);
      }else{
        const summary=page.locator('[data-library-context]>summary');
        assert((await summary.boundingBox()).height>=44,'touch-sized context');
        assert(await summary.innerText().then(t=>t.includes(state==='shared'?'Shared library:':'Personal library')),'selected context');
        await summary.focus();await page.keyboard.press('Enter');
        const select=page.locator('select[name=library_id]');
        assert(await select.isVisible(),'native switch choices visible');
        const selected=state==='shared'?'sample-shared':'sample-personal';
        assert.equal(await select.inputValue(),selected);
        const box=await page.locator('.library-context-panel').boundingBox();assert(box.x>=-1&&box.x+box.width<=width+1,'popover contained');
        await select.selectOption(selected==='sample-personal'?'sample-shared':'sample-personal');
        assert.equal(posts.length,0,'selection alone never switches');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('[data-library-context]').getAttribute('open'),null,'Escape closes');
        assert(await summary.evaluate(n=>n===document.activeElement),'focus returns to context');
        await summary.click();
        await page.screenshot({path:path.join(output,`${state}-${width}-open.png`)});
        await page.getByRole('button',{name:'Switch library',exact:true}).click();
        await page.waitForURL('**/plus/settings/household/select');
        assert.equal(posts.length,1,'only explicit submit sends selection');
        assert.equal(posts[0].path,'/plus/settings/household/select');
        assert.equal(posts[0].data.csrf_token,'test-csrf');assert.equal(posts[0].data.next,'/plus/home');
      }
      assert.deepEqual(errors,[]);
      receipt.checks.push({state,width,passed:true,providerCalls:0});await context.close();
    }
    receipt.status='PASS';
  }catch(e){receipt.status='FAILED';receipt.error=e.message;process.exitCode=1;}
  finally{await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify({status:receipt.status,checks:receipt.checks.length,error:receipt.error,output}));}
})();
