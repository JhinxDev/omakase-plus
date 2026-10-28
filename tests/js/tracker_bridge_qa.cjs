/* Real templates/styles with synthetic fixtures and fully intercepted requests. */
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict'),crypto=require('node:crypto'),{spawnSync}=require('node:child_process');
const opts={};for(let i=2;i<process.argv.length;i+=2){assert(['--root','--output','--python','--browser','--modules'].includes(process.argv[i]));opts[process.argv[i].slice(2)]=process.argv[i+1];}
const root=path.resolve(opts.root||path.join(__dirname,'../..')),out=path.resolve(opts.output||fs.mkdtempSync(path.join(os.tmpdir(),'omakase-tracker-qa-')));fs.mkdirSync(out,{recursive:true});
const python=opts.python||process.env.OMAKASE_QA_PYTHON||path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const generated=spawnSync(python,[path.join(root,'tests/plus/render_tracker_bridge.py'),'--root',root,'--output',out],{encoding:'utf8'});assert.equal(generated.status,0,generated.stderr);
const {chromium}=require(require.resolve('playwright',{paths:[opts.modules||process.env.OMAKASE_QA_NODE_MODULES||root]}));
let ownedBrowser;
(async()=>{
 const browser=ownedBrowser=await chromium.launch({headless:true,...(opts.browser?{executablePath:opts.browser}:{channel:'msedge'})});
 const ctx=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce',serviceWorkers:'block'});
 const errors=[],posts=[],checks=[];
 await ctx.route('**/*',async route=>{
  const req=route.request(),u=new URL(req.url());assert.equal(u.hostname,'127.0.0.1','No external request allowed');
  if(req.method()==='POST'){posts.push({path:u.pathname,body:req.postData()});const state=u.pathname.endsWith('/preview')?'review':u.pathname.endsWith('/apply')?'applied':'review';return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(out,state+'.html'))});}
  if(u.pathname.startsWith('/plus/api/'))return route.fulfill({contentType:'application/json',body:JSON.stringify({items:[],unread_count:0})});
  if(u.pathname.startsWith('/plus/static/')){
   const base=path.join(root,'src/omakase/plus/static'),file=path.resolve(base,u.pathname.slice('/plus/static/'.length));assert(file.startsWith(path.resolve(base)+path.sep));
   if(!fs.existsSync(file))return route.fulfill({status:404,body:'Missing fixture asset'});
   return route.fulfill({contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'application/javascript':file.endsWith('.woff2')?'font/woff2':file.endsWith('.svg')?'image/svg+xml':'image/png',body:fs.readFileSync(file)});
  }
  const file=path.join(out,(u.pathname.split('/').pop()||'setup')+'.html');return route.fulfill({contentType:'text/html',body:fs.readFileSync(file)});
 });
 const page=await ctx.newPage();page.on('pageerror',e=>errors.push(e.message));
 for(const width of [320,390,1440]){
  await page.setViewportSize({width,height:1000});
  for(const name of ['setup','connected','reading','review','applied','error']){
   await page.goto('http://127.0.0.1:9143/'+name);await page.evaluate(()=>document.fonts.ready);
   const geometry=await page.locator('.tracker-workspace').evaluate(el=>({overflow:document.documentElement.scrollWidth>innerWidth,h1:document.querySelectorAll('h1').length,unlabelled:[...el.querySelectorAll('select,input:not([type=hidden])')].filter(e=>!e.labels?.length).length,smallTargets:[...el.querySelectorAll('button,select,summary,.tracker-choice')].filter(e=>e.getClientRects().length&&e.getBoundingClientRect().height<44).map(e=>e.tagName),smallText:[...el.querySelectorAll('p,label,dt,dd')].filter(e=>e.getClientRects().length&&parseFloat(getComputedStyle(e).fontSize)<14).length}));
   if(geometry.overflow||geometry.h1!==1||geometry.unlabelled||geometry.smallTargets.length||geometry.smallText)console.error(JSON.stringify({name,width,geometry}));
   assert.equal(geometry.overflow,false,name+' overflow@'+width);assert.equal(geometry.h1,1);assert.equal(geometry.unlabelled,0);assert.deepEqual(geometry.smallTargets,[]);assert.equal(geometry.smallText,0);
   const contrast=await page.locator('.tracker-workspace').evaluate(el=>{function lum(c){const a=c.match(/[0-9.]+/g).slice(0,3).map(Number).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4});return a[0]*.2126+a[1]*.7152+a[2]*.0722;}return [...el.querySelectorAll('h1,h2,p,label,button,select,dt,dd,legend,summary,a')].filter(e=>e.getClientRects().length&&e.textContent.trim()&&!e.disabled).flatMap(e=>{let s=getComputedStyle(e),n=e,b=s.backgroundColor;while(b==='rgba(0, 0, 0, 0)'&&n.parentElement){n=n.parentElement;b=getComputedStyle(n).backgroundColor;}let x=lum(s.color),y=lum(b),ratio=(Math.max(x,y)+.05)/(Math.min(x,y)+.05),large=parseFloat(s.fontSize)>=24||(parseFloat(s.fontSize)>=18.66&&parseInt(s.fontWeight)>=700);return ratio<(large?3:4.5)?[{tag:e.tagName,ratio}]:[];});});
   assert.deepEqual(contrast,[],name+' text contrast@'+width);
   checks.push({name,width,...geometry,contrast});
   if((name==='review'&&width===390)||(name==='connected'&&width===1440))await page.screenshot({path:path.join(out,name+'-'+width+'.png'),fullPage:true});
  }
 }
 await page.goto('http://127.0.0.1:9143/error');assert(await page.getByRole('button',{name:'Pause ongoing updates',exact:true}).isVisible(),'A saved opt-in remains cancellable with worker disabled');
 await page.goto('http://127.0.0.1:9143/connected');assert.equal(posts.length,0,'No provider/mutation on GET');
 await page.getByRole('button',{name:'Read a sync preview',exact:true}).click();
 await page.locator('input[name=approved][value="0"]').check();await page.getByRole('button',{name:'Confirm selected changes',exact:true}).click();
 assert(await page.getByRole('status').innerText().then(t=>t.includes('Uncertain: 1')));
 assert.equal(posts.length,2);assert(posts[0].path.endsWith('/preview')&&posts[1].path.endsWith('/apply'));assert(posts.every(p=>p.body.includes('csrf_token=synthetic-csrf')));
 await page.locator('.tracker-workspace button').first().focus();assert(await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle==='solid'));
 assert.deepEqual(errors,[]);
 const owned=['src/omakase/plus/templates/tracker_bridge.html','src/omakase/plus/templates/_settings_mal.html','src/omakase/plus/static/tracker-bridge.css'];const hashes=Object.fromEntries(owned.map(n=>[n,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,n))).digest('hex')]));
 fs.writeFileSync(path.join(out,'BROWSER-RECEIPT.json'),JSON.stringify({status:'passed',layout_checks:checks.length,mocked_form_journeys:1,checks,source_hashes:hashes,actual_provider_requests:0,actual_user_data:false,errors},null,2));await browser.close();console.log(JSON.stringify({layout_checks:checks.length,mocked_form_journeys:1,errors:0,output:out}));
})().catch(async e=>{console.error(e.stack);if(ownedBrowser)await ownedBrowser.close();process.exitCode=1;});
