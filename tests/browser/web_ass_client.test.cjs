/* Isolated Edge smoke for real WASM/OffscreenCanvas, plus lifecycle boundaries.
 * OMAKASE_ASS_BASELINE points to the existing private 30s qualification MP4.
 * No fixture font or private media is copied into the repository.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const http = require('node:http');
const path = require('node:path');
const {test} = require('node:test');
const {qaRuntime, qaOutput} = require('../../scripts/qa-environment.cjs');
const {chromium} = require(path.join(qaRuntime(), 'playwright'));
const root = path.resolve(__dirname, '../..');
const staticRoot = path.join(root, 'src/omakase/plus/static');
const ass = `[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Liberation Sans,34,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,20,20,30,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:12.00,Default,,0,0,0,,Faithful ASS: timing and outline
Dialogue: 1,0:00:00.00,0:00:12.00,Default,,0,0,0,,{\\move(100,80,500,80)\\c&H00FFFF&}Moving sign
`;

test('serial derived ASS renders, seeks, clears and tears down without remote requests', {timeout: 45000}, async () => {
  const baseline = process.env.OMAKASE_ASS_BASELINE;
  assert(baseline && fs.existsSync(baseline), 'Set OMAKASE_ASS_BASELINE to the private qualification baseline');
  const out = qaOutput('web-ass');
  fs.mkdirSync(out, {recursive: true});
  const server = http.createServer((request, response) => {
    response.setHeader('Cross-Origin-Opener-Policy', 'same-origin');
    response.setHeader('Cross-Origin-Embedder-Policy', 'require-corp');
    const pathname = new URL(request.url, 'http://localhost').pathname;
    if (pathname === '/') {
      response.setHeader('Content-Type', 'text/html');
      response.end('<!doctype html><body style="margin:0;background:#111"><div id="frame" style="width:640px;height:360px;position:relative"><video muted playsinline preload="auto" style="width:100%;height:100%" src="/baseline.mp4"></video></div><script type="module">import {OmakaseASS} from "/static/web-ass.js"; window.errors=[]; window.ass=new OmakaseASS({video:document.querySelector("video"),container:document.querySelector("#frame"),onError:e=>errors.push(e.message)}); window.moduleReady=true;</script>');
      return;
    }
    if (pathname === '/test.ass') { response.setHeader('Content-Type', 'text/plain'); response.end(ass); return; }
    if (pathname === '/slow.ass') {
      response.setHeader('Content-Type', 'text/plain');
      const timer=setTimeout(()=>response.end(ass),9000);
      response.on('close',()=>clearTimeout(timer));
      return;
    }
    if (pathname === '/denied.ass') { response.statusCode = 401; response.setHeader('Content-Type', 'text/html'); response.end('login'); return; }
    const file = pathname === '/baseline.mp4' ? baseline : path.resolve(staticRoot, '.' + pathname.replace(/^\/static/, ''));
    if (pathname !== '/baseline.mp4' && (!pathname.startsWith('/static/') || !file.startsWith(staticRoot + path.sep))) { response.statusCode = 404; response.end(); return; }
    if (!fs.existsSync(file)) { response.statusCode = 404; response.end(); return; }
    response.setHeader('Content-Type', ({'.js':'application/javascript','.wasm':'application/wasm','.woff2':'font/woff2','.mp4':'video/mp4'})[path.extname(file)] || 'application/octet-stream');
    const size=fs.statSync(file).size;
    if (pathname === '/baseline.mp4' && request.headers.range) {
      const match=/bytes=(\d+)-(\d*)/.exec(request.headers.range);
      const start=Number(match[1]),end=match[2]?Number(match[2]):size-1;
      response.statusCode=206;
      response.setHeader('Accept-Ranges','bytes');
      response.setHeader('Content-Range',`bytes ${start}-${end}/${size}`);
      response.setHeader('Content-Length',end-start+1);
      fs.createReadStream(file,{start,end}).pipe(response);
    } else {
      response.setHeader('Content-Length',size);
      fs.createReadStream(file).pipe(response);
    }
  });
  await new Promise(resolve => server.listen(8130, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({channel:'msedge', headless:true});
    const page = await browser.newPage({viewport:{width:700,height:420}});
    const requests = [], failures = [], workers = [];
    page.on('request', request => requests.push(request.url()));
    page.on('pageerror', error => failures.push(error.message));
    page.on('console', message => console.log('BROWSER', message.type(), message.text()));
    page.on('worker', worker => workers.push(worker.url()));
    await page.goto('http://127.0.0.1:8130/');
    await page.waitForFunction(() => window.moduleReady && document.querySelector('video').readyState >= 2);
    assert.equal(await page.evaluate(() => crossOriginIsolated), true);
    await page.evaluate(async () => { const v=document.querySelector('video'); v.currentTime=2; await new Promise(r=>v.addEventListener('seeked',r,{once:true})); });
    await page.screenshot({path:path.join(out,'off.png')});
    assert.equal(await page.evaluate(() => ass.setTrack({url:'/test.ass'})), true);
    await page.waitForTimeout(400);
    await page.screenshot({path:path.join(out,'rendered.png')});
    assert.notDeepEqual(fs.readFileSync(path.join(out,'off.png')),fs.readFileSync(path.join(out,'rendered.png')));
    assert.equal(workers.length, 1, 'one renderer worker and no pthreads');
    await page.evaluate(async () => { const v=document.querySelector('video'); v.currentTime=8; await new Promise(r=>v.addEventListener('seeked',r,{once:true})); await ass.repaint(); });
    await page.waitForTimeout(200);
    await page.screenshot({path:path.join(out,'seeked.png')});
    assert.notDeepEqual(fs.readFileSync(path.join(out,'rendered.png')),fs.readFileSync(path.join(out,'seeked.png')));
    await page.evaluate(() => ass.clear());
    assert.equal(await page.locator('canvas').count(), 0);
    await assert.rejects(page.evaluate(() => ass.setTrack({url:'https://example.invalid/forbidden.ass'})), /same-origin/);
    await assert.rejects(page.evaluate(() => ass.setTrack({url:'/denied.ass'})), /401/);
    await assert.rejects(page.evaluate(() => ass.setTrack({url:'/test.ass',fonts:Array(17).fill('/static/ass/default.woff2')})), /configuration/);
    await assert.rejects(page.evaluate(() => ass.setTrack({url:'/slow.ass'})), /8 seconds/);
    assert.equal(page.workers().length,0);
    await page.evaluate(async () => { const first=ass.setTrack({url:'/test.ass'}); const off=ass.clear(); await Promise.all([first,off]); });
    assert.equal(await page.locator('canvas').count(), 0);
    await page.evaluate(() => ass.setTrack({url:'/test.ass'}));
    assert.equal(page.workers().length, 1);
    const advanced=await page.evaluate(async()=>{const v=ass.video;const start=v.currentTime;await v.play();await new Promise(resolve=>setTimeout(resolve,400));v.pause();return v.currentTime>start;});
    assert.equal(advanced,true,'native video clock advances while ASS active');
    await page.evaluate(() => ass.destroy());
    await page.waitForTimeout(300);
    assert.equal(page.workers().length, 0);
    assert.equal(await page.locator('canvas').count(), 0);
    assert.deepEqual(requests.filter(url=>!url.startsWith('http://127.0.0.1:8130/')), []);
    assert.deepEqual(failures, []);
    const sha256=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    fs.writeFileSync(path.join(out,'proof.json'),JSON.stringify({browser:browser.version(),requests,workers,failures,
      assets:JSON.parse(fs.readFileSync(path.join(root,'THIRD-PARTY/jassub-2.5.23/asset-manifest.json'))),
      wrapperSHA256:sha256(path.join(staticRoot,'web-ass.js')),
      screenshots:Object.fromEntries(['off.png','rendered.png','seeked.png'].map(name=>[name,sha256(path.join(out,name))])),
      crossOriginIsolated:true, maximumConcurrentWorkers:1, workersAfterDestroy:0,
      wasmHeapMaximumMiB:512,rendererThreads:1},null,2));
  } finally { await browser?.close(); await new Promise(resolve=>server.close(resolve)); }
});
