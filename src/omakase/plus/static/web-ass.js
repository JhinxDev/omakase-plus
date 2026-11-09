/* Omakase private ASS adapter. Renderer is derived from JASSUB 2.5.23.
 * Native rVFC + document COOP/COEP are mandatory. No plaintext conversion.
 */
import JASSUB from './ass/jassub.js';

const MiB = 1024 * 1024;
const assets = new URL('./ass/', import.meta.url);
const rendererOwners = new WeakMap();

function localURL(value) {
  const url = new URL(value, location.href);
  if (!['http:', 'https:'].includes(url.protocol) || url.origin !== location.origin || url.username || url.password) {
    throw new Error('ASS assets must use authenticated same-origin HTTP URLs.');
  }
  return url.href;
}

function retryDelay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const aborted = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', aborted); resolve(); }, milliseconds);
    signal.addEventListener('abort', aborted, {once: true});
  });
}

async function readBounded(url, maximum, kind, signal, deadline) {
  const target = localURL(url);
  let response;
  for (let attempt = 0; attempt < 3; attempt++) {
    signal.throwIfAborted();
    response = await fetch(target, {credentials: 'same-origin', mode: 'same-origin', redirect: 'error', cache: 'no-store', signal});
    if (response.ok) break;
    if (response.status === 429) {
      await response.body?.cancel().catch(() => {});
      const header = (response.headers.get('retry-after') || '').trim();
      const delay = /^[1-9][0-9]*$/.test(header) ? Number(header) * 1000 : NaN;
      // Only cached asset GETs retry; every attempt shares the track's 8s budget.
      if (attempt < 2 && Number.isSafeInteger(delay) && delay < deadline - performance.now()) {
        await retryDelay(delay, signal);
        continue;
      }
    }
    throw new Error('ASS asset access failed (' + response.status + ').');
  }
  const type = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
  const allowed = kind === 'ass' ? ['text/plain', 'text/x-ass', 'text/x-ssa', 'application/x-ass', 'application/octet-stream']
    : ['font/ttf', 'font/otf', 'font/woff', 'font/woff2', 'application/font-sfnt', 'application/octet-stream'];
  if (!allowed.includes(type)) throw new Error('Unexpected ASS asset content type.');
  if (Number(response.headers.get('content-length')) > maximum) throw new Error('ASS asset exceeds size limit.');
  const reader = response.body.getReader();
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > maximum) throw new Error('ASS asset exceeds size limit.');
      chunks.push(value);
    }
  } finally { await reader.cancel().catch(() => {}); }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  if (kind === 'font') {
    const signature = Array.from(result.subarray(0, 4)).map(x => String.fromCharCode(x)).join('');
    if (!['\u0000\u0001\u0000\u0000', 'OTTO', 'wOFF', 'wOF2', 'true'].includes(signature)) throw new Error('Invalid font signature.');
  }
  return result;
}

export class OmakaseASS {
  constructor({video, container, onError = () => {}}) {
    if (!video || !container) throw new Error('ASS needs video and overlay container.');
    if (rendererOwners.has(video)) throw new Error('Only one ASS controller may own a video.');
    this.video = video;
    this.container = container;
    this.onError = onError;
    this.generation = 0;
    this.destroyed = false;
    this.renderer = null;
    this.serial = Promise.resolve();
    this.onSeek = () => { this.repaint().catch(error => this.fail(error)); };
    video.addEventListener('seeked', this.onSeek);
    video.addEventListener('loadedmetadata', this.onSeek);
    rendererOwners.set(video, this);
  }

  async dispose() {
    const renderer = this.renderer;
    this.renderer = null;
    this.canvas?.remove();
    this.canvas = null;
    if (!renderer) return;
    // Pinned JASSUB destroy removes video listeners synchronously before awaiting
    // readiness. Retain the exact worker handle to terminate a failed startup too.
    const worker = renderer._worker;
    const cleanup = renderer.destroy().catch(() => {});
    await Promise.race([cleanup, new Promise(resolve => setTimeout(resolve, 200))]);
    worker?.terminate();
  }

  fail(error) {
    this.generation += 1;
    this.abort?.abort();
    this.dispose().catch(() => {});
    this.onError(error instanceof Error ? error : new Error('ASS renderer failed.'));
  }

  setTrack({url, fonts = [], timeOffset = 0}) {
    const generation = ++this.generation;
    this.abort?.abort();
    // Hide stale glyphs immediately, including while a prior startup settles.
    if (this.canvas) this.canvas.hidden = true;
    const task = async () => {
      await this.dispose();
      if (generation !== this.generation || this.destroyed) return false;
      const abort = new AbortController();
      this.abort = abort;
      const deadline = performance.now() + 8000;
      const timer = setTimeout(() => abort.abort(new Error('ASS load exceeded 8 seconds.')), 8000);
      try {
        if (!globalThis.crossOriginIsolated || typeof SharedArrayBuffer === 'undefined') throw new Error('Faithful ASS requires the isolated player page.');
        if (typeof this.video.requestVideoFrameCallback !== 'function' || typeof OffscreenCanvas === 'undefined') throw new Error('Faithful ASS is unsupported by this browser.');
        if (!Array.isArray(fonts) || fonts.length > 16 || !Number.isFinite(timeOffset)) throw new Error('Invalid ASS track configuration.');
        localURL(url);
        fonts.forEach(localURL);
        const script = new TextDecoder('utf-8', {fatal: true}).decode(await readBounded(url, 8 * MiB, 'ass', abort.signal, deadline));
        if (!/^\s*\[Script Info\]/im.test(script) || !/^\s*\[Events\]/im.test(script)) throw new Error('Invalid ASS script.');
        // JASSUB lazy availableFonts loading can leave a paused first frame blank.
        // Admit the bundled fallback in the awaited initial-font set as well.
        const fallback = await readBounded(new URL('default.woff2', assets), MiB, 'font', abort.signal, deadline);
        const fontBytes = [fallback];
        let fontTotal = fallback.byteLength;
        for (const font of fonts) {
          const bytes = await readBounded(font, Math.min(8 * MiB, 32 * MiB - fontTotal), 'font', abort.signal, deadline);
          fontTotal += bytes.byteLength;
          fontBytes.push(bytes);
        }
        if (generation !== this.generation || this.destroyed) return false;
        const canvas = document.createElement('canvas');
        canvas.className = 'omakase-ass-overlay';
        const platform = navigator.userAgentData?.platform || navigator.platform;
        if (/^Win/i.test(platform) && /(?:Chrome|Chromium)\//.test(navigator.userAgent)) {
          canvas.classList.add('omakase-ass-composited');
        }
        canvas.setAttribute('aria-hidden', 'true');
        Object.assign(canvas.style, {position: 'absolute', inset: '0', width: '100%', height: '100%', pointerEvents: 'none'});
        this.canvas = canvas;
        this.container.appendChild(canvas);
        const renderer = new JASSUB({video: this.video, canvas, subContent: script, fonts: fontBytes,
          workerUrl: localURL(new URL('worker.js', assets)), wasmUrl: localURL(new URL('jassub-worker.wasm', assets)),
          modernWasmUrl: localURL(new URL('jassub-worker-modern.wasm', assets)),
          availableFonts: {'liberation sans': fallback},
          // libass bitmap-cache limit is MiB; glyph limit is a glyph count.
          queryFonts: false, timeOffset, libassMemoryLimit: 32, libassGlyphLimit: 10000});
        this.renderer = renderer;
        renderer._worker.addEventListener('error', event => {
          if (this.renderer === renderer) this.fail(new Error(event.message || 'ASS worker failed.'));
        });
        const aborted = new Promise((_, reject) => {
          if (abort.signal.aborted) reject(abort.signal.reason);
          else abort.signal.addEventListener('abort', () => reject(abort.signal.reason), {once: true});
        });
        await Promise.race([renderer.ready, aborted]);
        if (generation !== this.generation || this.destroyed) { await this.dispose(); return false; }
        await Promise.race([this.repaint(), aborted]);
        return true;
      } catch (error) {
        await this.dispose();
        if (generation !== this.generation || this.destroyed) return false;
        this.onError(error);
        throw error;
      } finally { clearTimeout(timer); }
    };
    const result = this.serial.then(task, task);
    this.serial = result.catch(() => {});
    return result;
  }

  async repaint() {
    const renderer = this.renderer;
    if (!renderer || !this.video.videoWidth) return;
    await renderer.ready;
    if (this.renderer !== renderer) return;
    await renderer.resize();
    await renderer.manualRender({expectedDisplayTime: performance.now(), width: this.video.videoWidth,
      height: this.video.videoHeight, mediaTime: this.video.currentTime}, true);
  }

  async clear() {
    ++this.generation;
    this.abort?.abort();
    if (this.canvas) this.canvas.hidden = true;
    await this.serial;
    await this.dispose();
  }

  async destroy() {
    this.destroyed = true;
    this.video.removeEventListener('seeked', this.onSeek);
    this.video.removeEventListener('loadedmetadata', this.onSeek);
    await this.clear();
    rendererOwners.delete(this.video);
  }
}

window.OmakaseASS = OmakaseASS;
