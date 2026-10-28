const {qaRuntime,qaOutput,qaPython,qaBrowser}=require('../../scripts/qa-environment.cjs');
/* Actual header/search markup and search.js, isolated Edge, all responses intercepted. */
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const runtime=qaRuntime();
const {chromium}=require(require.resolve('playwright',{paths:[runtime]}));
const root=path.resolve(__dirname,'../..');
const output=process.env.OMAKASE_SEARCH_QA_OUTPUT||qaOutput('global_search');fs.mkdirSync(output,{recursive:true});
const html=JSON.parse(execFileSync(qaPython(),['-c',"import runpy,json; f=runpy.run_path('tests/plus/test_global_search_ui.py'); print(json.dumps({p:f['render_search'](active_page=p,kind='anime',query='') for p in ['home','movies','tv','search']}))"],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
const cards={tv:{id:1396,title:'Breaking Bad',media_type:'tv',format:'TV series',url:'/plus/tv/1396'},movie:{id:'tt0084787',title:'The Thing',media_type:'movie',format:'MOVIE',url:'/plus/movies/tt0084787'},anime:{id:21,title:'One Piece',media_type:'anime',format:'TV',url:'/plus/catalog/anime/21'}};
const result={sourceSha256:{},checks:[],limitations:['Autocomplete provider responses are intercepted fixtures. Real mixed-server behavior is verified by the offline authenticated route tests; no provider request or mutation is sent.']};
for(const file of ['src/omakase/plus/static/search.js','src/omakase/plus/templates/base.html','src/omakase/plus/templates/search.html'])result.sourceSha256[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex');
(async()=>{const browser=await chromium.launch(qaBrowser());try{
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});const page=await context.newPage();const errors=[],requests=[],writes=[];
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const request=route.request();const url=new URL(request.url());
  if(request.method()!=='GET'){writes.push(url.pathname);return route.abort('blockedbyclient');}
  if(url.origin!=='http://search.test')return route.abort('blockedbyclient');
  if(url.pathname==='/plus/search/suggestions'){
   const q=url.searchParams.get('q').toLowerCase(),kind=url.searchParams.get('kind');requests.push({query:q,kind});
   let payload,status=200;
   if(q==='offline'){status=503;payload={results:[],warnings:[{kind:'tv',label:'TV',detail:'TV unavailable.'}]};}
   else if(q==='limited'){status=429;payload={results:[],detail:'Too many catalog searches.',retry_after:60};}
   else if(q==='partial')payload={results:[cards.tv],warnings:[{kind:'movies',label:'Movies',detail:'Movies unavailable.',retry_after:60}]};
   else payload={results:[kind==='anime'?cards.anime:q.includes('thing')?cards.movie:q.includes('piece')?cards.anime:cards.tv],warnings:[]};
   return route.fulfill({status,contentType:'application/json',body:JSON.stringify(payload)});
  }
  const name=url.pathname.split('/').pop();
  if(['home','movies','tv','search'].includes(name))return route.fulfill({contentType:'text/html',body:html[name]});
  if(url.pathname.startsWith('/plus/static/')){const file=path.resolve(root,'src/omakase/plus/static',url.pathname.slice('/plus/static/'.length));if(file.startsWith(path.join(root,'src/omakase/plus/static')+path.sep)&&fs.existsSync(file))return route.fulfill({path:file});}
  if(Object.values(cards).some(card=>url.pathname===card.url))return route.fulfill({contentType:'text/html',body:'<h1>Selected exact catalog identity</h1>'});
  return route.fulfill({status:404,body:''});
 });
 for(const active of ['home','movies','tv']){
  await page.goto('http://search.test/plus/'+active,{waitUntil:'networkidle'});
  const header=page.locator('.header-search');assert.equal(await header.locator('[data-search-kind]').inputValue(),'all');
  for(const [query,card,label] of [['breaking bad',cards.tv,'TV'],['the thing',cards.movie,'Movie'],['one piece',cards.anime,'Anime']]){
   const input=header.locator('[data-search-input]');await input.fill(query);
   const option=header.getByRole('option').first();await option.waitFor({state:'visible'});await page.waitForFunction(title=>document.querySelector('.header-search [role=option] strong')?.textContent===title,card.title);
   assert((await option.innerText()).includes(label));assert.equal(requests.at(-1).kind,'all');if(active==='home'&&query==='breaking bad')await page.screenshot({path:path.join(output,'home-breaking-bad.png'),fullPage:false});
   await input.press('ArrowDown');assert.equal(await input.getAttribute('aria-activedescendant'),await option.getAttribute('id'));await input.press('Escape');assert.equal(await input.getAttribute('aria-expanded'),'false');
   result.checks.push({page:active,query,kind:'all',visibleType:label});
  }
 }
 const header=page.locator('.header-search'),input=header.locator('[data-search-input]');
 await input.fill('offline');await page.waitForFunction(()=>document.querySelector('.header-search [data-search-status]')?.textContent.includes('Search incomplete'));
 assert(!(await header.innerText()).includes('No suggestions'));assert((await header.locator('[data-search-status]').innerText()).includes('TV unavailable'));
 await input.fill('limited');await page.waitForFunction(()=>document.querySelector('.header-search [data-search-status]')?.textContent.includes('60 seconds'));assert(!(await header.innerText()).includes('No suggestions'));
 await input.fill('partial');await header.getByRole('option').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('.header-search [role=status]')?.textContent.includes('Movies unavailable')||[...document.querySelectorAll('.header-search li')].some(n=>n.textContent.includes('Movies unavailable')));
 assert((await header.locator('[data-search-suggestions]').innerText()).includes('Movies unavailable'));await page.screenshot({path:path.join(output,'partial-search.png'),fullPage:false});result.checks.push({partial:true,unavailable:true,rateLimitVisible:true});
 await input.press('ArrowDown');await input.press('Enter');await page.waitForURL('**/plus/tv/1396');result.checks.push({keyboardSelection:'/plus/tv/1396'});
 await page.goto('http://search.test/plus/search',{waitUntil:'networkidle'});const scoped=page.locator('.catalog-search-form');assert.equal(await scoped.locator('[data-search-kind]').inputValue(),'anime');await scoped.locator('[data-search-input]').fill('one piece');await scoped.locator('[data-search-suggestions] [role=option]').waitFor({state:'visible'});assert.equal(requests.at(-1).kind,'anime');result.checks.push({explicitScope:'anime'});
 await page.screenshot({path:path.join(output,'scoped-search-desktop.png'),fullPage:false});assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);result.errors=errors;result.writes=writes;result.requests=requests;
 await context.close();
}finally{await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(result,null,2));}console.log(JSON.stringify({passed:result.checks.length,output}));})().catch(error=>{console.error(error);process.exitCode=1;});
