/* Source-bound rendered review, exclusively against the guarded loopback showcase. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {parseArgs} = require('node:util');
const {qaRuntime, qaBrowser} = require('./qa-environment.cjs');
const {chromium} = require(require.resolve('playwright', {paths:[qaRuntime()]}));
const {values} = parseArgs({options:{url:{type:'string'},output:{type:'string'},widths:{type:'string',default:'1440,390,320'},routes:{type:'string'}}});
const base = new URL(values.url);
assert(base.protocol === 'http:' && base.hostname === '127.0.0.1' && base.port, 'guarded loopback URL required');
assert(!base.username && !base.password && !base.search && !base.hash && base.pathname === '/', 'origin only');
const out = path.resolve(values.output);
assert(!fs.existsSync(out), 'new evidence directory required');
const widths = values.widths.split(',').map(Number);
assert(widths.every(w => [320,390,1440].includes(w)), 'finite review widths');
const root = path.resolve(__dirname,'..');
const files = ['templates/base.html','templates/_library_context.html','templates/_collection_search.html','templates/library.html','templates/_media_components.html','static/household.css','static/cinema.css','static/dashboard.js'];
const receipt = {status:'RUNNING',at:new Date().toISOString(),origin:base.origin,sourceSha256:{},pages:[],blocked:[],pageErrors:[]};
for (const file of files) receipt.sourceSha256[file] = crypto.createHash('sha256').update(fs.readFileSync(path.join(root,'src/omakase/plus',file))).digest('hex');
receipt.runnerSha256 = crypto.createHash('sha256').update(fs.readFileSync(path.join(root,'scripts/showcase_preview.py'))).digest('hex');
fs.mkdirSync(out,{recursive:true});
const routes = {
  home:'/plus/home',anime:'/plus/library',movies:'/plus/movies',tv:'/plus/tv',
  title:'/plus/demo/states/title/populated',history:'/plus/activity',settings:'/plus/settings',
  language:'/plus/settings/languages',household:'/plus/settings/household',tracking:'/plus/settings/tracking',search:'/plus/search',
  'anime-more':'/plus/library','anime-type':'/plus/library?content_type=series'
};
const selectedNames=values.routes?values.routes.split(','):Object.keys(routes);
assert(selectedNames.every(name=>Object.hasOwn(routes,name)),'known review routes');
(async()=>{
  const browser = await chromium.launch(qaBrowser());
  try {
    for (const width of widths) {
      const context = await browser.newContext({viewport:{width,height:width===1440?900:844},reducedMotion:'reduce',serviceWorkers:'block'});
      await context.route('**/*',route=>{
        const request=route.request();
        if(new URL(request.url()).origin===base.origin && ['GET','HEAD'].includes(request.method())) return route.continue();
        receipt.blocked.push({method:request.method(),path:new URL(request.url()).pathname});
        return route.abort();
      });
      const page=await context.newPage();
      page.on('pageerror',e=>receipt.pageErrors.push({width,error:e.message}));
      for (const name of selectedNames) {
        const route=routes[name];
        const response = await page.goto(base.origin+route,{waitUntil:'networkidle',timeout:20000});
        assert.equal(response.status(),200,`${name} GET`);
        assert.equal(response.headers()['x-omakase-demo'],'real-media-sample-account-read-only','synthetic response');
        assert(await page.getByRole('complementary',{name:'Review environment'}).count(),'visible disclosure');
        if(name==='anime-more')await page.locator('.watchlist-filter-details>summary').click();
        // Full-page screenshots must observe native lazy-image loading first.
        await page.evaluate(async()=>{
          await document.fonts.ready;
          for(let y=0,step=0;y<document.documentElement.scrollHeight&&step<60;y+=innerHeight*.75,step++) {
            scrollTo(0,y);
            await new Promise(resolve=>setTimeout(resolve,70));
            await Promise.all([...document.images].filter(n=>{
              const r=n.getBoundingClientRect();return r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;
            }).map(n=>Promise.race([n.decode().catch(()=>{}),new Promise(resolve=>setTimeout(resolve,1500))])));
          }
          scrollTo(0,0);
        });
        const facts=await page.evaluate(()=>{
          const box=n=>n?.getBoundingClientRect().toJSON()??null;
          const main=document.querySelector('main');
          return {overflow:document.documentElement.scrollWidth-innerWidth,heading:box(document.querySelector('h1')),
            scope:box(document.querySelector('.library-scope-bar,.library-context')),
            toolbar:box(document.querySelector('.collection-search')),
            firstArtwork:box(main?.querySelector('article img[data-poster],.cinema-hero-art img,.library-poster img,.movie-poster img,.tv-card img')),
            firstPreference:box(document.querySelector('.language-field')),
            decodedImages:[...document.images].filter(n=>n.complete&&n.naturalWidth>0).length,
            headingText:document.querySelector('h1')?.textContent.trim()};
        });
        await page.screenshot({path:path.join(out,`${name}-${width}.png`),fullPage:true});
        receipt.pages.push({name,width,route,...facts});
        console.log(`${name} ${width}: heading y${Math.round(facts.heading?.y??0)}, art y${Math.round(facts.firstArtwork?.y??0)}`);
      }
      await context.close();
    }
    assert.equal(receipt.blocked.length,0,'no external/mutating requests attempted');
    assert.equal(receipt.pageErrors.length,0,'no page errors');
    receipt.status='PASS';
  } catch(e) {
    receipt.status='FAILED';receipt.error=e.message;process.exitCode=1;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify(receipt,null,2)+'\n');
    console.log(JSON.stringify({status:receipt.status,pages:receipt.pages.length,error:receipt.error,output:out}));
  }
})();
