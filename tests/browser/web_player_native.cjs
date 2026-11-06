/* Opt-in, existing-authorized-media review. Never downloads/acquires a title. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {qaRuntime, qaBrowser} = require('../../scripts/qa-environment.cjs');
const {chromium} = require(path.join(qaRuntime(), 'playwright'));
const base = process.env.OMAKASE_PLAYER_REVIEW_URL?.replace(/\/$/,'');
const retainedReview = process.env.OMAKASE_PLAYER_RETAINED_REVIEW === 'true';
const remainder = process.env.OMAKASE_PLAYER_NATIVE_REMAINDER === 'true';
if (!base) throw new Error('Set the isolated authenticated review URL explicitly.');
const output = process.env.OMAKASE_PLAYER_QA_OUTPUT || path.join(require('node:os').tmpdir(), 'omakase-player-native');
fs.mkdirSync(output,{recursive:true});
const assert = (ok,message) => {if(!ok) throw new Error(message);};
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
(async()=>{
  const browser=await chromium.launch(qaBrowser());
  const report={url:base,synthetic:false,subjectiveAudio:false,qualification:retainedReview?'composite retained-copy plus metadata repair':'original preparation and browser',browser:browser.version(),errors:[],events:[],responses:[]};
  if(remainder)report.reusedChecks={receipt:process.env.OMAKASE_PLAYER_PRIOR_PROOF,scope:'Prior successful decoded distant seeks and exact original font deliveries; this run covers caption timing/fullscreen, audio choice and resume.'};
  let page,session;
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
    await context.addInitScript(()=>{const play=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(...args){this.muted=true;return play.apply(this,args);};});
    page=await context.newPage();
    page.on('pageerror',e=>report.errors.push(e.message));
    page.on('response',async response=>{
      const pathname=new URL(response.url()).pathname;
      if(!pathname.includes('/api/playback/'))return;
      report.responses.push({path:pathname.replace(/\/sessions\/[^/]+/,'/sessions/<session>'),status:response.status()});
      if(response.request().method()==='POST' && [201,202].includes(response.status())) session=await response.json();
      if(pathname.endsWith('/events')){
        const body=response.request().postDataJSON(),saved=await response.json().catch(()=>({}));
        report.events.push({status:response.status(),event:body.event,playing:body.playing,position:body.position_seconds,rate:body.playback_rate,audio:body.audio_id,text:body.text_id,credited:saved.credited_seconds});
      }
    });
    await page.goto(base);
    await page.getByRole('link',{name:/Enter the .* review account/}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-player-start]').disabled);
    assert(await page.locator('video').evaluate(v=>v.paused&&v.currentTime===0),'No autoplay');
    report.before=await(await page.request.get(base+'/review/evidence')).json();
    const clicked=Date.now();
    await page.getByRole('button',{name:'Play episode',exact:true}).click();
    if(!retainedReview){
      await page.waitForFunction(()=>document.querySelector('[data-player-cancel-preparation]')?.hidden===false || !document.querySelector('[data-player-error]').hidden,null,{timeout:15000});
      report.preparing=await page.evaluate(()=>({cancel:!!document.querySelector('[data-player-cancel-preparation]')&&!document.querySelector('[data-player-cancel-preparation]').hidden,plex:!document.querySelector('[data-player-plex]').hidden,status:document.querySelector('[data-player-status]').textContent}));
      assert(report.preparing.cancel&&report.preparing.plex,'Cancel and exact Plex fallback before copy');
    }
    await page.waitForFunction(()=>{const v=document.querySelector('video');return (v.videoWidth>0&&v.getVideoPlaybackQuality().totalVideoFrames>0&&v.currentTime>.5)||!document.querySelector('[data-player-error]').hidden;},null,{timeout:75000});
    report.preparationResultMs=Date.now()-clicked;
    report.first=await page.locator('video').evaluate(v=>({time:v.currentTime,width:v.videoWidth,height:v.videoHeight,frames:v.getVideoPlaybackQuality().totalVideoFrames,duration:v.duration,muted:v.muted}));
    if(report.first.frames>0&&report.first.width>0)report.clickToFirstDecodedMs=report.preparationResultMs;
    report.evidence=await(await page.request.get(base+'/review/evidence')).json();
    report.playerFeedback=await page.evaluate(()=>({error:document.querySelector('[data-player-error]')?.textContent,status:document.querySelector('[data-player-status]')?.textContent}));
    assert(report.first.frames>0&&report.first.width>0,'Actual source must decode');
    assert(report.errors.length===0,'No page errors');
    assert(session?.source.mode==='copy-remux','Real copy route');
    report.tracks={source:session.source.tracks,subtitles:session.subtitles.map(s=>({id:s.plex_stream_id,language:s.language,codec:s.codec})),fonts:session.fonts.length,map:session.track_map};
    await page.locator('video').evaluate(v=>v.pause());
    await page.waitForTimeout(1000);
    report.seeks=[];
    for(const fraction of remainder?[]:[.7,.1,.99]){
      const target=report.first.duration*fraction;
      const framesBefore=await page.locator('video').evaluate(v=>v.getVideoPlaybackQuality().totalVideoFrames);
      await page.locator('video').evaluate((v,t)=>{v.currentTime=t;},target);
      await page.waitForFunction(t=>{const v=document.querySelector('video');return Math.abs(v.currentTime-t)<1&&v.readyState>=2;},target,{timeout:15000});
      await page.waitForFunction(n=>document.querySelector('video').getVideoPlaybackQuality().totalVideoFrames>n,framesBefore,{timeout:15000});
      report.seeks.push(await page.locator('video').evaluate(v=>({time:v.currentTime,ready:v.readyState,frames:v.getVideoPlaybackQuality().totalVideoFrames})));
    }
    const english=session.subtitles.find(s=>/^(en|eng)$/.test(s.language))||session.subtitles[0];
    assert(english,'Actual styled subtitle track required');
    report.fonts=[];
    for(const url of remainder?[]:session.fonts){
      const response=await page.request.get(new URL(url,base).href);
      assert(response.status()===200,'Authenticated original font delivery');
      const bytes=await response.body(),filename=new URL(url,base).pathname.split('/').at(-1);
      const expected=report.evidence.qualification.fonts.find(f=>f.filename===filename);
      assert(expected&&sha(bytes)===expected.sha256&&bytes.length===expected.bytes,'Exact extracted original font bytes');
      report.fonts.push({filename,bytes:bytes.length,sha256:sha(bytes)});
    }
    const assResponse=await page.request.get(new URL(english.url,base).href);
    assert(assResponse.status()===200,'Authenticated ASS delivery');
    const assBytes=await assResponse.body(),ass=assBytes.toString('utf8');
    const seconds=value=>value.split(':').reduce((a,b)=>a*60+Number(b),0);
    const cues=ass.split(/\r?\n/).filter(line=>line.startsWith('Dialogue:')).map(line=>{
      const fields=line.split(',');return {start:seconds(fields[1]),end:seconds(fields[2]),style:fields[3],text:fields.slice(9).join(',')};
    }).filter(c=>c.end-c.start>=1&&c.text.replace(/\{[^}]*\}/g,'').trim()&&!/\\p[1-9]/.test(c.text));
    const selectedCue=cues.find(c=>c.start>60&&c.end<report.first.duration-30)||cues[0];
    assert(selectedCue,'A nonempty sustained actual subtitle cue is required');
    const cue=selectedCue.start+Math.min(.5,(selectedCue.end-selectedCue.start)/2);
    report.ass={sha256:sha(assBytes),cue,activeFrom:selectedCue.start,activeUntil:selectedCue.end,style:selectedCue.style,textSha256:sha(Buffer.from(selectedCue.text)),fonts:session.fonts.length};
    await page.getByLabel('Styled subtitles',{exact:true}).selectOption('ass:'+english.plex_stream_id);
    await page.locator('video').evaluate((v,t)=>{v.currentTime=t;},cue);
    await page.waitForFunction(t=>{const v=document.querySelector('video');return Math.abs(v.currentTime-t)<.1&&v.readyState>=2&&!v.seeking;},cue,{timeout:15000});
    await page.waitForFunction(()=>document.querySelector('[data-player-ass-status]').textContent==='Styled subtitles ready.'&&!!document.querySelector('canvas.omakase-ass-overlay'),null,{timeout:12000});
    await page.waitForTimeout(800);
    await page.locator('[data-player-container]').screenshot({path:path.join(output,'native-ass-on.png')});
    await page.locator('canvas.omakase-ass-overlay').screenshot({path:path.join(output,'native-ass-canvas.png')});
    report.ass.canvasData=await page.locator('canvas.omakase-ass-overlay').evaluate(canvas=>{try{return canvas.toDataURL('image/png');}catch{return null;}});
    if(report.ass.canvasData){fs.writeFileSync(path.join(output,'native-ass-transparent.png'),Buffer.from(report.ass.canvasData.split(',')[1],'base64'));delete report.ass.canvasData;}
    await page.locator('[data-player-container]').evaluate(node=>node.requestFullscreen());
    await page.waitForFunction(()=>!!document.fullscreenElement);
    await page.waitForTimeout(400);
    await page.screenshot({path:path.join(output,'native-ass-fullscreen.png')});
    await page.evaluate(()=>document.exitFullscreen());
    await page.getByLabel('Styled subtitles',{exact:true}).selectOption('off');
    await page.waitForFunction(()=>!document.querySelector('canvas.omakase-ass-overlay'));
    await page.locator('[data-player-container]').screenshot({path:path.join(output,'native-ass-off.png')});
    await page.locator('[data-player-container]').hover();
    await page.locator('.shaka-overflow-menu-button').click({force:true});
    await page.locator('.shaka-language-button').click({force:true});
    const audio=page.locator('.shaka-audio-languages');
    report.audioChoices=await audio.locator('button').allTextContents();
    report.audioAccessibleLabels=await audio.locator('button').evaluateAll(nodes=>nodes.map(node=>({text:node.textContent,ariaLabel:node.getAttribute('aria-label'),role:node.getAttribute('role')})));
    const choices=audio.locator('button').filter({hasText:/English|Japanese/});
    assert(await choices.count()>=2,'Two actual audio choices');
    for(const language of ['Japanese','English']){
      await page.locator('[data-player-container]').hover();
      if(!(await audio.isVisible())){
        const languageButton=page.locator('.shaka-language-button');
        if(!(await languageButton.isVisible()))await page.locator('.shaka-overflow-menu-button').click();
        await languageButton.waitFor({state:'visible'});
        await languageButton.click();
        await audio.waitFor({state:'visible'});
      }
      await audio.getByRole('menuitemradio',{name:language,exact:true}).click();
      await page.locator('video').evaluate(v=>v.play());
      await page.waitForTimeout(1300);
      await page.locator('video').evaluate(v=>v.pause());
      await page.waitForTimeout(800);
    }
    const paused=await page.locator('video').evaluate(v=>v.currentTime);
    report.savedAudioIDs=[...new Set(report.events.map(e=>e.audio).filter(v=>v!=null))];
    assert(report.savedAudioIDs.length>=2,'Audio changes persist by exact player track');
    await page.reload();
    await page.waitForFunction(()=>!document.querySelector('[data-player-start]').disabled);
    await page.getByRole('button',{name:'Play episode',exact:true}).click();
    await page.waitForFunction(t=>{const v=document.querySelector('video');return v.getVideoPlaybackQuality().totalVideoFrames>0&&Math.abs(v.currentTime-t)<3;},paused,{timeout:20000});
    report.resume=await page.locator('video').evaluate(v=>v.currentTime);
    assert(Math.abs(report.resume-paused)<3,'Exact real-source resume');
    await page.locator('video').evaluate(v=>v.pause());
    await page.waitForTimeout(1000);
    await page.setViewportSize({width:390,height:844});
    await page.locator('[data-player-container]').screenshot({path:path.join(output,'native-phone.png')});
    report.phone=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
    assert(report.phone.scroll<=report.phone.width+1,'Phone width');
    report.evidence=await(await page.request.get(base+'/review/evidence')).json();
    const qualified=report.evidence.qualification;
    const copyProof=retainedReview?qualified.metadata_review.retained_proof:qualified;
    assert(copyProof.copy_jobs===1&&copyProof.probe_jobs===1&&copyProof.observer_jobs===1,'One original copy and observer only; resume reuses immutable READY');
    if(retainedReview){
      assert(qualified.copy_jobs===0&&qualified.probe_jobs===0&&qualified.observer_jobs===0,'Retained review launches no media jobs');
      assert(qualified.metadata_review.new_media_jobs===0&&qualified.metadata_review.all_retained_bytes_verified&&qualified.metadata_review.original_ready_unchanged,'Explicit unchanged retained-copy seam');
      assert(JSON.stringify(qualified.metadata_review.changed_resources)==='["manifest.mpd"]','Only codec manifest metadata changed');
    }
    assert(!qualified.telemetry_errors?.length,'No qualification telemetry errors');
    assert(copyProof.phases.length===3&&copyProof.phases.every(p=>p.returncode===0),'Both full-input children and prefix exit successfully');
    assert(copyProof.phases.filter(p=>p.phase!=='probe').every(p=>p.input_bytes===qualified.expected_size),'Both workers consume the complete original');
    assert(report.events.every(e=>e.status===200),'All native events accepted');
    assert(report.errors.length===0,'No runtime page errors');
    report.passed=true;
    await context.close();
  }catch(error){report.passed=false;report.failure=error.stack;if(page)report.evidence=await(await page.request.get(base+'/review/evidence')).json().catch(()=>null);throw error;}
  finally{fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify({passed:report.passed,failure:report.failure,clickToFirstDecodedMs:report.clickToFirstDecodedMs,first:report.first,output}));}
})();
