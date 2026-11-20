/* Isolated home progress semantics, aligned card progress and remote-safe actions. */
const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');
const {chromium} = require(require.resolve('playwright', {paths:[qaRuntime()]}));
const root = path.resolve(__dirname, '..');
const output = process.env.OMAKASE_HOME_QA_OUTPUT || qaOutput('home_progress');
fs.mkdirSync(output, {recursive:true});
const fixture = `import json
from scripts.preview_plus_workspace import render
titles=[]
for i,(name,watched,resume,total) in enumerate([('Partial episode',6,{'minutes_left':23,'percent':8},12),('Ready for next episode',1,None,12),('First episode in progress',0,{'minutes_left':18,'percent':25},12),('Total not known',0,{'minutes_left':20,'percent':12},None)],1):
    titles.append(dict(id=i,anilist_id=i,title=name,progress_episodes=watched,progress_percent=watched/12*100,play_episode=watched+1,next_episode=watched+1,episode_total=total,episode_resume=resume,partial_playback=bool(resume),new_episode_available=not resume,play_url=f'/plus/watch/anime/{i}?episode={watched+1}',last_source_label='Manual',native_resume_at=f'2026-11-{20-i:02d}T16:00:00Z'))
print(json.dumps(render('home',home_titles=titles)))`;
const html=JSON.parse(execFileSync(qaPython(),['-c',fixture],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
const staticRoot=path.join(root,'src/omakase/plus/static');
const evidence={sourceSha256:{},checks:[]};
const rgba=value=>{const n=value.match(/[\d.]+/g).map(Number);return [...n.slice(0,3),n[3]??1];};
const luminance=rgb=>rgb.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
const contrast=(foreground,background)=>{const f=luminance(foreground),b=luminance(background);return(Math.max(f,b)+.05)/(Math.min(f,b)+.05);};
for(const file of ['templates/home.html','templates/_home_components.html','static/product-shell.css'])
  evidence.sourceSha256[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,'src/omakase/plus',file))).digest('hex');
function verifyProgress(metrics, scenario) {
 assert(metrics.every(m=>m.episodeVisible),'episode and remaining-time text are visible on every card');
 assert(metrics.every(m=>m.metaWidth<=m.cardWidth+1 && m.metaScroll<=m.metaWidth+1),'episode metadata fits its own card');
 for(const [index,value] of [50,100/12,0].entries())assert(Math.abs(metrics[index].barValue-value)<.001);
 assert(metrics.slice(0,3).every(m=>m.watched?.startsWith('Watched')&&m.barLabel?.includes('episodes watched')),'every known-total bar describes series watched progress');
 assert.equal(metrics[3].barValue,undefined,'unknown totals do not invent a series percentage');
 assert.match(metrics[0].episode,/Episode 7.*23 min left/s);
 assert.match(metrics[1].episode,/Episode 2.*Up next/s);
 assert.match(metrics[2].episode,/Episode 1.*18 min left/s);
 assert.match(metrics[3].episode,/Episode 1.*20 min left/s);
 if(!scenario.textScale)assert(Math.max(...metrics.slice(0,3).map(m=>m.barTop))-Math.min(...metrics.slice(0,3).map(m=>m.barTop))<=2,'known-total watched bars align across resume and next-episode cards');
}
(async()=>{
 const browser=await chromium.launch(qaBrowser());
 try {
  for(const scenario of [{width:320},{width:390},{width:1440},{width:2560},{width:390,textScale:2},{width:390,whiteArtwork:true},{width:1440,whiteArtwork:true}]) {
   const context=await browser.newContext({viewport:{width:scenario.width,height:scenario.width<=390?844:1000},reducedMotion:'reduce'});
   const page=await context.newPage();
   await page.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/plus/home')return route.fulfill({contentType:'text/html',body:html});
    if(url.pathname.startsWith('/plus/static/')) {
     const file=path.resolve(staticRoot,url.pathname.slice('/plus/static/'.length));
     if(file.startsWith(staticRoot+path.sep)&&fs.existsSync(file))return route.fulfill({path:file});
    }
    if(/\/(poster|banner)$/.test(url.pathname))return route.fulfill({contentType:'image/svg+xml',body:scenario.whiteArtwork?'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#fff"/></svg>':'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#343146"/><path d="M0 400L300 80L800 450" fill="#62577c"/></svg>'});
    return route.fulfill({status:404,body:''});
   });
   await page.goto('http://home.test/plus/home',{waitUntil:'networkidle'});
   if(scenario.textScale)await page.addStyleTag({content:'html{font-size:200%}'});
   const row=page.locator('[data-home-row="continue-anime"]');
   const cards=row.locator('.om-card');
   assert.equal(await cards.count(),4);
   const readMetrics=()=>cards.evaluateAll(nodes=>nodes.map(node=>{
    const meta=node.querySelector('.om-card-meta'), episode=meta?.querySelector('.om-card-episode');
    const watched=meta?.querySelector(':scope > .om-card-context'), bar=node.querySelector('progress');
    const rect=node.getBoundingClientRect();
    return {episode:episode?.textContent.trim(),episodeVisible:!!episode&&getComputedStyle(episode).display!=='none'&&episode.getBoundingClientRect().height>0,
      watched:watched?.textContent.trim().replace(/\s+/g,' '),barValue:bar?.value,barLabel:bar?.getAttribute('aria-label'),
      barTop:bar?bar.getBoundingClientRect().top-rect.top:null,cardWidth:rect.width,
      metaWidth:meta?.getBoundingClientRect().width,metaScroll:meta?.scrollWidth};
   }));
   const metrics=await readMetrics();
   verifyProgress(metrics,scenario);
   if(scenario.width===320) {
    // A captured playhead must not turn into watched series progress for unknown totals.
    const injected=await cards.last().evaluateHandle(card=>{
     const bar=document.createElement('progress');
     bar.value=.12;
     card.querySelector('.om-card-cover').append(bar);
     return bar;
    });
    const wrongProgress=await readMetrics();
    assert.throws(()=>verifyProgress(wrongProgress,scenario),/unknown totals do not invent/);
    await injected.evaluate(bar=>bar.remove());
   }
   const pageWidth=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,outside:[...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth+1&&!n.closest('.om-rail, .media-rail')).slice(0,8).map(n=>({tag:n.tagName,class:n.className,right:n.getBoundingClientRect().right}))}));
   const suffix=`${scenario.textScale?'-text200':''}${scenario.whiteArtwork?'-white':''}`;
   await page.screenshot({path:path.join(output,`home-${scenario.width}${suffix}-layout.png`),fullPage:true});
   assert(pageWidth.scroll<=pageWidth.width+1,'rail scroll stays inside the page: '+JSON.stringify(pageWidth));
   const play=cards.first().locator('.om-card-link');
   await page.keyboard.press('Tab');
   await play.focus();
   assert.equal(await play.getAttribute('href'),'/plus/watch/anime/1?episode=7');
   assert.match(await play.getAttribute('aria-label'),/Partial episode, resume at saved position/);
   assert.notEqual(await play.evaluate(node=>getComputedStyle(node.parentElement,'::after').boxShadow),'none','keyboard action has visible focus');
   let overlayContrast;
   if(scenario.whiteArtwork) {
    const samples=await cards.evaluateAll(nodes=>nodes.map(node=>{
     const link=node.querySelector('.om-card-link'), cover=link.getBoundingClientRect(), cue=link.querySelector('.om-card-play');
     const cueStyle=getComputedStyle(cue);
     const metadata=[...node.querySelectorAll('.om-card-meta .om-card-context, .om-card-meta .om-card-status')];
     const pageBackground=getComputedStyle(document.body).backgroundColor;
     return{background:cueStyle.backgroundColor,foreground:cueStyle.color,cueTop:cue.getBoundingClientRect().top-cover.top,
       coverHeight:cover.height,coverWidth:cover.width,pageBackground,metadata:metadata.map(label=>getComputedStyle(label).color)};
    }));
    overlayContrast=samples.map(sample=>{const bg=rgba(sample.background);const white=bg.slice(0,3).map(v=>v*bg[3]+255*(1-bg[3]));return{...sample,ratio:contrast(rgba(sample.foreground).slice(0,3),white),textRatios:sample.metadata.map(color=>contrast(rgba(color).slice(0,3),rgba(sample.pageBackground).slice(0,3)))};});
    assert(overlayContrast.every(s=>s.ratio>=3),'play icon has non-text contrast over white artwork');
    assert(overlayContrast.every(s=>rgba(s.pageBackground)[3]===1&&s.textRatios.every(ratio=>ratio>=4.5)),'episode/remaining text meets AA below the artwork');
    assert(overlayContrast.every(s=>s.cueTop>=s.coverHeight*.5),'upper half of artwork stays clear of the play cue');
    assert(overlayContrast.every(s=>s.coverHeight>=44&&s.coverWidth>=44),'the whole cover remains the touch target');
   }
   await page.screenshot({path:path.join(output,`home-${scenario.width}${suffix}.png`),fullPage:true});
   await row.screenshot({path:path.join(output,`continue-${scenario.width}${suffix}.png`)});
   evidence.checks.push({...scenario,metrics,overlayContrast});await context.close();
  }
 } finally {await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(evidence,null,2));}
 console.log(JSON.stringify({passed:evidence.checks.length,output}));
})().catch(error=>{console.error(error);process.exitCode=1;});
