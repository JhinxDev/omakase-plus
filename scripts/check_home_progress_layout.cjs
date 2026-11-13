/* Isolated home progress semantics, aligned card footers and remote-safe actions. */
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
const fixture = `import json,runpy
from pathlib import Path
from jinja2 import Environment,FileSystemLoader,select_autoescape
suite=runpy.run_path('tests/plus/test_streaming_templates.py')
base=suite['title_context']()
cards=[]
for i,(name,watched,resume,total) in enumerate([('Partial episode',6,{'minutes_left':23,'percent':8},12),('Ready for next episode',1,None,12),('First episode in progress',0,{'minutes_left':18,'percent':25},12),('Total not known',0,{'minutes_left':20,'percent':12},None)]):
    cards.append({**base,'id':i+1,'anilist_id':i+1,'title':name,'progress_episodes':watched,'progress_percent':watched/12*100,'play_episode':watched+1,'next_episode':watched+1,'episode_total':total,'episode_resume':resume,'partial_playback':bool(resume),'play_label':'Resume' if resume else 'Play','play_url':'/plus/watch/anime/'+str(i+1),'last_source_label':'Manual'})
env=Environment(loader=FileSystemLoader(Path('src/omakase/plus/templates')),autoescape=select_autoescape(['html']))
print(json.dumps(env.get_template('home.html').render(csrf_token='fixture',email='viewer@example.test',active_page='home',asset_version='fixture',continue_watching=cards,ready_to_watch=[],confirmation_needed=[],new_episodes=[],attention=[],recent_activity=[],watch_history=[])))`;
const html=JSON.parse(execFileSync(qaPython(),['-c',fixture],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
const staticRoot=path.join(root,'src/omakase/plus/static');
const evidence={sourceSha256:{},checks:[]};
const rgba=value=>{const n=value.match(/[\d.]+/g).map(Number);return [...n.slice(0,3),n[3]??1];};
const luminance=rgb=>rgb.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
const contrast=(foreground,background)=>{const f=luminance(foreground),b=luminance(background);return(Math.max(f,b)+.05)/(Math.min(f,b)+.05);};
for(const file of ['templates/_media_components.html','static/cinema.css','static/refinement.css'])
  evidence.sourceSha256[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,'src/omakase/plus',file))).digest('hex');
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
   const cards=page.locator('.watching-rail .media-card');
   assert.equal(await cards.count(),4);
   const metrics=await cards.evaluateAll(nodes=>nodes.map(node=>{
    const episode=node.querySelector('.continue-episode');const bar=node.querySelector('progress');const watched=node.querySelector('.watched-count');
    const rect=node.getBoundingClientRect();const style=getComputedStyle(episode);
    return {episode:episode.textContent.trim(),episodeVisible:style.display!=='none'&&episode.getBoundingClientRect().height>0,watched:watched?.textContent.trim().replace(/\s+/g,' '),barValue:bar?.value,barLabel:bar?.getAttribute('aria-label'),barTop:bar?bar.getBoundingClientRect().top-rect.top:null,cardWidth:rect.width,episodeWidth:episode.scrollWidth};
   }));
   assert(metrics.every(m=>m.episodeVisible),'episode and remaining-time text are visible on every card');
   assert(metrics.every(m=>m.episodeWidth<=m.cardWidth+1),'episode metadata fits its own card');
   for(const [index,value] of [50,100/12,0].entries())assert(Math.abs(metrics[index].barValue-value)<.001);
   assert(metrics.slice(0,3).every(m=>m.watched?.startsWith('Watched')&&m.barLabel?.includes('episodes watched')),'every known-total bar describes series watched progress');
   assert.equal(metrics[3].barValue,undefined,'unknown totals do not invent a series percentage');
   assert(metrics[0].episode.includes('23 min left')&&metrics[1].episode.includes('Up next'));
   if(!scenario.textScale)assert(Math.max(...metrics.slice(0,3).map(m=>m.barTop))-Math.min(...metrics.slice(0,3).map(m=>m.barTop))<=2,'known-total watched bars align across resume and next-episode cards');
   const pageWidth=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,outside:[...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth+1&&!n.closest('.media-rail')).slice(0,8).map(n=>({tag:n.tagName,class:n.className,right:n.getBoundingClientRect().right}))}));
   const suffix=`${scenario.textScale?'-text200':''}${scenario.whiteArtwork?'-white':''}`;
   await page.screenshot({path:path.join(output,`home-${scenario.width}${suffix}-layout.png`),fullPage:true});
   assert(pageWidth.scroll<=pageWidth.width+1,'rail scroll stays inside the page: '+JSON.stringify(pageWidth));
   const play=cards.first().locator('.media-card-cover');await play.focus();
   assert.equal(await play.getAttribute('href'),'/plus/watch/anime/1');
   assert.match(await play.getAttribute('aria-label'),/Resume Partial episode, episode 7/);
   assert.notEqual(await play.evaluate(node=>getComputedStyle(node).outlineStyle),'none','keyboard action has visible focus');
   let overlayContrast;
   if(scenario.whiteArtwork) {
    const samples=await cards.evaluateAll(nodes=>nodes.map(node=>{const cover=node.querySelector('.media-card-cover').getBoundingClientRect();const cue=node.querySelector('.media-card-transport');const label=cue.querySelector('span');return{artHeight:cover.height,cueTop:cue.getBoundingClientRect().top-cover.top,background:getComputedStyle(cue).backgroundColor,foreground:getComputedStyle(label).color,secondary:getComputedStyle(cue.querySelector('small')).color,coverHeight:cover.height,coverWidth:cover.width};}));
    overlayContrast=samples.map(sample=>{const bg=rgba(sample.background);const white=bg.slice(0,3).map(v=>v*bg[3]+255*(1-bg[3]));return{...sample,primaryRatio:contrast(rgba(sample.foreground).slice(0,3),white),secondaryRatio:contrast(rgba(sample.secondary).slice(0,3),white)};});
    assert(overlayContrast.every(s=>s.primaryRatio>=4.5&&s.secondaryRatio>=4.5),'play and episode labels meet AA over white artwork');
    assert(overlayContrast.every(s=>s.cueTop>=s.artHeight*.6),'upper60percent of artwork stays clear of the dark cue');
    assert(overlayContrast.every(s=>s.coverHeight>=44&&s.coverWidth>=44),'the whole cover remains the touch target');
   }
   await page.screenshot({path:path.join(output,`home-${scenario.width}${suffix}.png`),fullPage:true});
   await page.locator('.continue-section').screenshot({path:path.join(output,`continue-${scenario.width}${suffix}.png`)});
   evidence.checks.push({...scenario,metrics,overlayContrast});await context.close();
  }
 } finally {await browser.close();fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(evidence,null,2));}
 console.log(JSON.stringify({passed:evidence.checks.length,output}));
})().catch(error=>{console.error(error);process.exitCode=1;});
