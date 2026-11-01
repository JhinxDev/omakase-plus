'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventQueue, selectedTracks, browserCapabilities, subtitlePreference, audioPreference, eventPosition, playbackRateConfiguration, styledSubtitleTracks, enhanceRangeControls, SessionPreparation, startPayload, exitSnapshot, TerminalCheckpoint, enforcePlayback, guardedPlay, nextEpisodeUrl, subtitleChoices, initialSubtitleValue, subtitleMenuKey, switchSubtitleRenderer } = require('../../src/omakase/plus/static/web-player.js');

test('supplied and native playback remain stopped after failure until a qualified explicit recovery', async () => {
  let pauses=0, plays=0;
  const video={pause(){pauses++;}};
  let state={ready:false,failed:true,leaving:false,captionLoading:false};
  const play=guardedPlay(video,async()=>{plays++;},()=>state);
  await play();assert.equal(plays,0);assert.equal(pauses,1);
  assert.equal(enforcePlayback(video,state),false);
  state={...state,failed:false};await play();assert.equal(plays,0,'Recovery setup does not authorize old buffered playback');
  state={...state,ready:true,captionLoading:true};await play();assert.equal(plays,0);
  state={...state,captionLoading:false};await play();assert.equal(plays,1);
  state={...state,leaving:true};await play();assert.equal(plays,1);
});

function fixtureStorage() {
  const values=new Map();
  return {values,getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};
}
const finalExit={seq:7,event:'seek',position_seconds:3,playing:false,playback_rate:1,audio_id:null,text_id:null};

test('one tab exit checkpoint is bounded, short-lived and excludes provider/auth fields', () => {
  const storage=fixtureStorage();let now=1000;
  const checkpoint=new TerminalCheckpoint(storage,()=>now);
  const record=checkpoint.write('opaque-session',finalExit);
  assert.deepEqual(Object.keys(record),['session_id','final_event','expires_at']);
  assert.equal(record.expires_at,61000);
  checkpoint.write('new-session',finalExit);
  assert.equal(storage.values.size,1);
  assert.ok(new TextEncoder().encode([...storage.values.values()][0]).length<=4096);
  assert.equal(checkpoint.write('session',{...finalExit,provider_url:'private'}),null);
  now=61001;
  assert.deepEqual(checkpoint.read(),{unconfirmed:true});
  assert.equal(storage.values.size,0);
});

test('the next explicit start waits for exact terminal204 acknowledgement and clears only that checkpoint', async () => {
  const storage=fixtureStorage();const checkpoint=new TerminalCheckpoint(storage,()=>1000);
  checkpoint.write('opaque-session',finalExit);
  let release;const gate=new Promise(resolve=>{release=resolve;});const order=[];
  const run=(async()=>{await checkpoint.flush(async(path,body,_keepalive,_timeout,_signal,status)=>{
    order.push('close');assert.equal(path,'/plus/api/playback/sessions/opaque-session/close');assert.deepEqual(body,{final_event:finalExit});assert.equal(status,204);await gate;
  });order.push('start');})();
  await Promise.resolve();assert.deepEqual(order,['close']);release();await run;
  assert.deepEqual(order,['close','start']);assert.equal(storage.values.size,0);
});

test('auth/revision conflicts keep the pending receipt and prevent a new start; late old ACK cannot clear newer state', async () => {
  const storage=fixtureStorage();const checkpoint=new TerminalCheckpoint(storage,()=>1000);
  const old=checkpoint.write('old-session',finalExit);let started=false;
  await assert.rejects(async()=>{await checkpoint.flush(async()=>{throw Object.assign(new Error('conflict'),{status:409});});started=true;},/conflict/);
  assert.equal(started,false);assert.equal(storage.values.size,1);
  checkpoint.write('new-session',{...finalExit,seq:8});checkpoint.clear(old);
  assert.equal(checkpoint.read().record.session_id,'new-session');
});

test('unavailable tab storage stays best effort and never claims a confirmed prior exit', async () => {
  const checkpoint=new TerminalCheckpoint({getItem(){throw new Error('unavailable');},setItem(){throw new Error('unavailable');}},()=>1000);
  assert.equal(checkpoint.write('session',finalExit),null);
  assert.deepEqual(await checkpoint.flush(()=>{throw new Error('no record should send');}),{unconfirmed:true});
});

test('deferred source preparation is an explicit single bounded request; direct sources bypass it', async () => {
  const calls = [];
  const direct = {session_id:'direct',media_url:'/media'};
  const request = async (...args) => { calls.push(args); return {session_id:'pending',media_url:'/ready'}; };
  assert.strictEqual(await new SessionPreparation(request,direct).resolve(), direct);
  assert.equal(calls.length, 0);
  const pending = {session_id:'pending',preparation_required:true,prepare_url:'/plus/api/playback/sessions/pending/prepare'};
  const prepared = await new SessionPreparation(request,pending).resolve();
  assert.equal(prepared.media_url, '/ready');
  assert.deepEqual(calls[0].slice(0,4), [pending.prepare_url,{},false,55000]);
  assert.ok(calls[0][4] instanceof AbortSignal);
  assert.deepEqual(startPayload({monitor_id:1,episode_number:2,library_id:'library',next_url:'/next'}, {codecs:['h264'],ass_supported:false}),
    {monitor_id:1,episode_number:2,library_id:'library',codecs:['h264'],ass_supported:false,defer_prepare:true});
});

test('cancelling pending preparation aborts it and closes the exact session once, without retry', async () => {
  const calls = [];
  const pending = {session_id:'pending',preparation_required:true,prepare_url:'/plus/api/playback/sessions/pending/prepare'};
  const request = (...args) => {
    calls.push(args);
    if (args[0].endsWith('/close')) return Promise.resolve({});
    return new Promise((_resolve,reject) => args[4].addEventListener('abort',()=>reject(new DOMException('cancelled','AbortError'))));
  };
  const operation = new SessionPreparation(request,pending);
  const result = operation.resolve();
  const rejection = assert.rejects(result, {name:'AbortError'});
  await operation.cancel(); await operation.cancel(); await rejection;
  assert.equal(calls.length,2);
  assert.equal(calls[1][0],'/plus/api/playback/sessions/pending/close');
});

test('late preparation results cannot resume a cancelled operation; failures do not start another job', async () => {
  const pending = {session_id:'pending',preparation_required:true,prepare_url:'/plus/api/playback/sessions/pending/prepare'};
  let finish;
  const operation = new SessionPreparation(path => path.endsWith('/close') ? Promise.resolve({}) : new Promise(resolve => {finish=resolve;}),pending);
  const result = operation.resolve();
  await operation.cancel(true);
  finish({session_id:'pending',media_url:'/ready'});
  await assert.rejects(result,{name:'AbortError'});
  let attempts = 0;
  const failing = new SessionPreparation(async () => {attempts++;throw new Error('unsupported');},pending);
  await assert.rejects(failing.resolve(),/unsupported/);
  assert.equal(attempts,1);
});

test('styled subtitle admission treats SSA and ASS equally', () => {
  assert.deepEqual(styledSubtitleTracks([{codec:'SSA',plex_stream_id:1}, {codec:'ass',plex_stream_id:2}, {codec:'srt',plex_stream_id:3}]).map(track=>track.plex_stream_id), [1,2]);
});

test('supplied range enhancement names speed and protects native navigation without swallowing Escape or defaults', () => {
  class Range extends EventTarget {
    constructor() { super(); this.attributes = new Map(); this.value = '1'; this.classList = {contains: name => name === 'shaka-playback-rate-slider'}; }
    getAttribute(name) { return this.attributes.get(name); }
    setAttribute(name, value) { this.attributes.set(name, value); }
  }
  const range = new Range();
  const container = {querySelectorAll: selector => selector.startsWith('input') ? [range] : []};
  enhanceRangeControls(container);
  enhanceRangeControls(container);
  assert.equal(range.getAttribute('aria-label'), 'Playback speed');
  for (const key of ['End','Home','ArrowRight','ArrowLeft','PageUp','PageDown']) {
    const event = new Event('keydown', {cancelable:true});
    event.key = key;
    range.dispatchEvent(event);
    assert.equal(event.cancelBubble, true);
    assert.equal(event.defaultPrevented, false);
  }
  const escape = new Event('keydown', {cancelable:true}); escape.key = 'Escape';
  range.dispatchEvent(escape);
  assert.equal(escape.cancelBubble, false);
  range.value = '2'; range.dispatchEvent(new Event('input'));
  assert.equal(range.getAttribute('aria-valuetext'), '2 times normal speed');
  const seek = new Range();
  seek.classList = {contains: name => name === 'shaka-seek-bar'};
  enhanceRangeControls({querySelectorAll: selector => selector.startsWith('input') ? [seek] : []});
  const seekEnd = new Event('keydown', {cancelable:true}); seekEnd.key = 'End';
  seek.dispatchEvent(seekEnd);
  assert.equal(seekEnd.cancelBubble, false, 'Keep Shaka’s seek start/commit lifecycle');
});

test('event positions use the pinned server timeline across mux rounding and backward seeks', () => {
  assert.equal(eventPosition(1800.02, 1800), 1800);
  assert.equal(eventPosition(40, 1800), 40);
  assert.equal(eventPosition(0, 1800), 0);
  assert.equal(eventPosition(-0.02, 1800), 0);
  for (const invalid of [NaN, Infinity, -Infinity]) assert.equal(eventPosition(invalid, 1800), null);
  assert.equal(eventPosition(1, undefined), null);
  assert.equal(eventPosition(1, 0), null);
});

test('Shaka speed presets and continuous slider stay within the server accepted interval', () => {
  const config = playbackRateConfiguration();
  assert.equal(config.playbackRateSliderMin, 0.25);
  assert.equal(config.playbackRateSliderMax, 2);
  assert.ok(config.playbackRates.includes(1));
  assert.ok(config.playbackRates.every(rate => rate >= config.playbackRateSliderMin && rate <= config.playbackRateSliderMax));
  assert.equal(config.playbackRateSliderStep, 0.05);
});

test('capability reporting uses actual MP4 MSE admission and all native ASS prerequisites', () => {
  const scope = {MediaSource: {isTypeSupported: type => type.includes('hvc1') || type.includes('opus')}, crossOriginIsolated: true, SharedArrayBuffer: function(){}, OffscreenCanvas: function(){}, Worker: function(){}};
  const video = {requestVideoFrameCallback(){}};
  assert.deepEqual(browserCapabilities(scope, video), {codecs: ['hevc', 'opus'], ass_supported: true});
  scope.crossOriginIsolated = false;
  assert.equal(browserCapabilities(scope, video).ass_supported, false);
  scope.MediaSource.isTypeSupported = () => { throw new Error('unsupported'); };
  assert.deepEqual(browserCapabilities(scope, video).codecs, []);
});

test('ASS preference preserves Off and exact-source identity, then uses explicit source flags', () => {
  const tracks = [{plex_stream_id:'10',language:'eng'}, {plex_stream_id:'11',language:'jpn',forced:true}, {plex_stream_id:'12',selected:true}];
  assert.equal(subtitlePreference(tracks, 'off'), 'off');
  assert.equal(subtitlePreference(tracks, 'ass:10'), 'ass:10');
  assert.equal(subtitlePreference(tracks, 'off', true), 'ass:12');
  tracks.pop();
  assert.equal(subtitlePreference(tracks, null), 'ass:11');
  tracks[1].forced = false;
  assert.equal(subtitlePreference(tracks, null), 'ass:10');
});

test('DASH audio restoration bridges exact original representation IDs without language guesses', () => {
  const variants = [{id:1,audioId:21,videoId:5,originalAudioId:'audio-ja',active:true}, {id:2,audioId:22,videoId:5,originalAudioId:'audio-en'}];
  const player = {getAudioTracks:()=>[{language:'ja',active:true}], getVariantTracks:()=>variants};
  assert.equal(selectedTracks({...player,getTextTracks:()=>[]}).audio_id, '21');
  assert.equal(audioPreference(player,{audio_id:'22'}).variant.id, 2);
  assert.equal(audioPreference(player,{track_map:[{kind:'audio',representation_id:'audio-en',selected:true}]}).variant.id, 2);
  assert.equal(audioPreference(player,{track_map:[{kind:'audio',language:'en',selected:true}]}), null);
  variants.push({...variants[1],id:3});
  assert.equal(audioPreference(player,{audio_id:'22'}), null);
});

test('Shaka 5 track selection uses supported public APIs and preserves subtitle Off', () => {
  const player = {getAudioTracks: () => [{id: 17, active: true}], getTextTracks: () => [{id: 23, active: false}]};
  assert.deepEqual(selectedTracks(player), {audio_id: '17', text_id: 'off'});
  player.getTextTracks = () => [{id: 23, active: true}];
  assert.deepEqual(selectedTracks(player), {audio_id: '17', text_id: '23'});
});

test('events are serialized and preserve deliberate backwards seek and terminal state', async () => {
  const received = [];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const queue = new EventQueue(async payload => {
    received.push(payload);
    if (received.length === 1) await gate;
  }, error => { throw error; });
  queue.push({ event: 'play', position_seconds: 100 });
  queue.push({ event: 'progress', position_seconds: 105 });
  queue.push({ event: 'progress', position_seconds: 110 });
  queue.push({ event: 'seek', position_seconds: 0 });
  queue.push({ event: 'pause', position_seconds: 1, playing: false });
  assert.equal(received.length, 1);
  release();
  await queue.idle();
  assert.deepEqual(received.map(p => [p.seq, p.event, p.position_seconds]), [[1, 'play', 100], [2, 'progress', 110], [3, 'seek', 0], [4, 'pause', 1]]);
});

test('one transport retry reuses the exact sequence and payload', async () => {
  const received = [];
  const queue = new EventQueue(async payload => { received.push(payload); if (received.length === 1) throw new Error('transport'); }, error => { throw error; });
  queue.push({ event: 'pause', position_seconds: 30 });
  await queue.idle();
  assert.equal(received.length, 2);
  assert.strictEqual(received[0], received[1]);
});

test('access conflicts stop the queue without retry or later writes', async () => {
  let calls = 0;
  let failure;
  const queue = new EventQueue(async () => { calls++; throw Object.assign(new Error('conflict'), { status: 409 }); }, error => { failure = error; });
  queue.push({ event: 'play' });
  queue.push({ event: 'pause' });
  await queue.idle();
  queue.push({ event: 'play' });
  assert.equal(calls, 1);
  assert.equal(failure.status, 409);
});

test('stalled event buffers fail closed at their finite bound', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let failure;
  const queue = new EventQueue(async () => gate, error => { failure = error; });
  queue.push({ event: 'play' });
  for (let i = 0; i < 13; i++) queue.push({ event: 'seek', position_seconds: i });
  assert.equal(queue.pending.length, 0);
  assert.equal(queue.stopped, true);
  assert.match(failure.message, /could not keep up/);
  release();
  await queue.idle();
});

test('closely spaced semantic events respect the configured server write interval', async () => {
  const times = [];
  const queue = new EventQueue(async () => { times.push(Date.now()); }, error => { throw error; }, () => {}, 30);
  queue.push({ event: 'seek' });
  queue.push({ event: 'play' });
  await queue.idle();
  assert.equal(times.length, 2);
  assert.ok(times[1] - times[0] >= 29);
});

test('delayed live anchors sample after pacing, then freeze the same payload across retry', async () => {
  const received = [];
  let livePosition = 1;
  let samples = 0;
  const queue = new EventQueue(async payload => {
    received.push(payload);
    if (received.length === 2) { livePosition = 999; throw new Error('transport'); }
  }, error => { throw error; }, () => {}, 30);
  queue.push({event:'seek',position_seconds:0,playing:false});
  queue.push({event:'play',position_seconds:1,playing:true}, false, () => {
    samples++;
    return {event:'play',position_seconds:livePosition,playing:true};
  });
  livePosition = 4;
  await queue.idle();
  assert.equal(samples, 1);
  assert.equal(received[1].position_seconds, 4, 'Use the actual clock after the queue delay');
  assert.strictEqual(received[1], received[2], 'A retry must retain the first attempt’s exact snapshot');
  assert.equal(received[2].seq, 2);
});

test('an invalidated live sampler retains the captured boundary and never reads beyond a seek', async () => {
  const received = [];
  let epoch = 1;
  const queue = new EventQueue(async payload => received.push(payload), error => { throw error; }, () => {}, 30);
  queue.push({event:'seek',position_seconds:0,playing:false});
  queue.push({event:'progress',position_seconds:2,playing:true}, false, () => epoch === 1 ? {event:'progress',position_seconds:999} : null);
  epoch++;
  queue.push({event:'seek',position_seconds:20,playing:false});
  queue.push({event:'pause',position_seconds:20,playing:false});
  await queue.idle();
  assert.deepEqual(received.map(payload=>payload.position_seconds), [0,2,20,20]);
  assert.deepEqual(received.map(payload=>payload.seq), [1,2,3,4]);
});

test('old acknowledgements never claim a newer queued backward seek is saved', async () => {
  let release;
  const first = new Promise(resolve=>{release=resolve;});
  const feedback = [];
  const queue = new EventQueue(async payload => {if(payload.seq===1)await first;}, error=>{throw error;}, (_result,state)=>feedback.push(state.latest));
  queue.push({event:'seek',position_seconds:27,playing:false});
  queue.push({event:'seek',position_seconds:3,playing:false});
  release();await queue.idle();
  assert.deepEqual(feedback,[false,true]);
});

test('unsent paused seek bursts compact to the newest target while preserving paced dispatch order', async () => {
  const received = [];
  const queue = new EventQueue(async payload=>received.push(payload),error=>{throw error;},()=>{},40);
  queue.push({event:'pause',position_seconds:2,playing:false});
  await queue.idle();
  queue.push({event:'seek',position_seconds:15,playing:false});
  queue.push({event:'seek',position_seconds:27,playing:false});
  queue.push({event:'seek',position_seconds:3,playing:false});
  await queue.idle();
  assert.deepEqual(received.map(p=>[p.seq,p.position_seconds]),[[1,2],[2,3]]);
});

test('sealing an exit supersedes unsent seeks with one immutable higher sequence and suppresses late feedback', async () => {
  let reject;
  const inFlight = new Promise((_resolve,fail)=>{reject=fail;});
  const sent = [], saved = [], errors = [];
  const queue = new EventQueue(async payload=>{sent.push(payload);await inFlight;},error=>errors.push(error),result=>saved.push(result));
  queue.push({event:'progress',position_seconds:27,playing:false});
  queue.push({event:'seek',position_seconds:3,playing:false});
  const final = queue.seal(exitSnapshot({position_seconds:3,playing:false,playback_rate:1},queue));
  assert.deepEqual(final,{position_seconds:3,playing:false,playback_rate:1,event:'seek',seq:2});
  assert.strictEqual(queue.seal({event:'pause',position_seconds:99,playing:false}),final);
  assert.ok(Object.isFrozen(final));
  queue.push({event:'seek',position_seconds:27,playing:false});
  reject(Object.assign(new Error('closed by final event'),{status:409}));
  await queue.idle();
  assert.equal(sent.length,1);
  assert.equal(queue.pending.length,0);
  assert.deepEqual(errors,[]);
  assert.deepEqual(saved,[]);
});

test('terminal snapshots never credit a pending seek as an ended or pause interval', async () => {
  const queue = new EventQueue(async()=>{},error=>{throw error;});
  queue.push({event:'seek',position_seconds:3,playing:false});
  assert.equal(exitSnapshot({position_seconds:3},queue,false,true).event,'seek');
  await queue.idle();
  assert.equal(exitSnapshot({position_seconds:3},queue).event,'pause');
  assert.equal(exitSnapshot({position_seconds:30},queue,false,true).event,'ended');
  assert.equal(exitSnapshot({position_seconds:3},queue,true).event,'seek');
});


test('direct Next accepts only the following episode in the same series and library', () => {
 const c={monitor_id:7,episode_number:2,library_id:'a'},o='https://app.example';
 assert.equal(nextEpisodeUrl('/plus/watch/7/3?library_id=a',c,o),o+'/plus/watch/7/3?library_id=a');
 for(const p of ['/plus/watch/8/3?library_id=a','/plus/watch/7/4?library_id=a','/plus/watch/7/3?library_id=b','/plus/watch/7/3?library_id=a&library_id=a','/plus/watch/7/3?library_id=a&x=1','/plus/watch/7/3?library_id=a#x','https://app.example/plus/watch/7/3?library_id=a','/plus/watch/7/2/../3?library_id=a',null]) assert.equal(nextEpisodeUrl(p,c,o),null,p);
});
test('one subtitle menu restores native ID zero, ASS and Off without cross-version saved choices', () => {
 const n=[{id:0,language:'en',active:true},{id:8,language:'ja',forced:true}],a=[{plex_stream_id:12,language:'ja',selected:true,default:true}];
 const choices=subtitleChoices(n,a);assert.deepEqual(choices.map(c=>c.value),['off','native:0','native:8','ass:12']);assert.match(choices[2].label,/Forced/);assert.match(choices[3].label,/Styled ASS.*Default/);
 assert.equal(initialSubtitleValue(n,a,'off'),'off');assert.equal(initialSubtitleValue(n,a,'0'),'native:0');assert.equal(initialSubtitleValue(n,a,'ass:12'),'ass:12');assert.equal(initialSubtitleValue(n,a,'off',true),'ass:12');assert.equal(initialSubtitleValue(n,[],null),'native:0');assert.equal(initialSubtitleValue([],[],null),'off');
});
test('subtitle handoff clears styled before native and disables native before styled loading',async()=>{
 const log=[];let release;const gate=new Promise(r=>release=r);
 const o={nativeTracks:[{id:0}],styledTracks:[{plex_stream_id:12}],current:()=>true,clearStyled:async()=>{log.push('clear');await gate;},selectNative:t=>log.push(['native',t?.id??null]),setCustom:v=>log.push(['custom',v]),loadStyled:async()=>{log.push('load');return true;}};
 const pending=switchSubtitleRenderer('native:0',o);await Promise.resolve();assert.deepEqual(log,['clear']);release();assert.equal(await pending,true);assert.deepEqual(log,['clear',['custom',null],['native',0]]);
 log.length=0;assert.equal(await switchSubtitleRenderer('ass:12',o),true);assert.deepEqual(log,[['custom','off'],['native',null],'load',['custom','ass:12']]);
 log.length=0;assert.equal(await switchSubtitleRenderer('off',o),true);assert.deepEqual(log,['clear',['custom','off'],['native',null]]);await assert.rejects(switchSubtitleRenderer('native:99',o),/unavailable/);
});
test('stale asynchronous subtitle intent cannot activate its track',async()=>{
 const calls=[];let current=true,release;const o={nativeTracks:[{id:3}],styledTracks:[],current:()=>current,clearStyled:()=>new Promise(r=>release=r),selectNative:t=>calls.push(t),setCustom:v=>calls.push(v)};
 const p=switchSubtitleRenderer('native:3',o);current=false;release();assert.equal(await p,false);assert.deepEqual(calls,[]);
});
test('subtitle native select retains arrow defaults and Escape closes with focus return',()=>{
 let closed=0;const event=key=>({key,target:{tagName:'SELECT'},preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}});
 const arrow=event('ArrowDown');subtitleMenuKey(arrow,()=>closed++);assert.equal(arrow.stopped,true);assert.equal(arrow.prevented,undefined);assert.equal(closed,0);
 const escape=event('Escape');subtitleMenuKey(escape,()=>closed++);assert.equal(escape.prevented,true);assert.equal(escape.stopped,true);assert.equal(closed,1);
 const other=event('ArrowRight');other.target.tagName='BUTTON';subtitleMenuKey(other,()=>closed++);assert.equal(other.stopped,undefined);
 for(const shiftKey of [false,true]) { const tab=event('Tab');tab.shiftKey=shiftKey;subtitleMenuKey(tab,()=>closed++);assert.equal(tab.stopped,undefined,'Tab reaches supplied Shaka focus containment');assert.equal(tab.prevented,undefined); }
 for(const key of ['Shift','Control','Alt','Meta']) { const modifier=event(key);subtitleMenuKey(modifier,()=>closed++);assert.equal(modifier.stopped,undefined,'Modifier transition reaches supplied Shaka pressed-key state'); }
 for(const key of ['Tab','Shift','k','ArrowDown','Escape']) { const release=event(key);release.type='keyup';subtitleMenuKey(release,()=>closed++);assert.equal(release.stopped,undefined,'Every key release can clear supplied Shaka pressed-key state');assert.equal(release.prevented,undefined); }
});
