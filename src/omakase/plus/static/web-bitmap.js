const EVENTS = ['loadedmetadata', 'timeupdate', 'pause', 'play', 'seeked'];
const MAX_WINDOW_BYTES = 2_150_000;

function validWindow(window, position) {
  return Number.isFinite(window?.start) && Number.isFinite(window.end)
    && window.start >= 0 && window.start <= position && position < window.end
    && Array.isArray(window.cues) && window.cues.length <= 128
    && window.cues.every(cue => {
      if (!Array.isArray(cue?.box) || cue.box.length !== 4) return false;
      const [x, y, w, h] = cue.box;
      return Number.isFinite(cue.start) && Number.isFinite(cue.end)
        && window.start <= cue.start && cue.start < cue.end && cue.end <= window.end + .000001
        && cue.box.every(value => Number.isFinite(value) && value >= 0 && value <= 1)
        && w > 0 && h > 0 && x + w <= 1.000001 && y + h <= 1.000001
        && typeof cue.image === 'string' && cue.image.length <= MAX_WINDOW_BYTES
        && cue.image.startsWith('iVBORw0KGgo');
    });
}

const BACKOFF_MS = [2000, 4000, 8000, 15000];

export class BitmapSubtitles {
  constructor({ video, container, url, onStatus = null }) {
    this.video = video;
    this.container = container;
    this.url = url;
    this.onStatus = typeof onStatus === 'function' ? onStatus : null;
    this.status = 'idle';
    this.image = document.createElement('img');
    this.image.className = 'web-bitmap-overlay';
    this.image.alt = '';
    this.image.setAttribute('aria-hidden', 'true');
    this.image.hidden = true;
    this.windows = [];
    this.pending = null;
    this.retryAt = 0;
    this.retryTimer = null;
    this.failures = 0;
    this.frame = null;
    this.cue = null;
    this.update = this.update.bind(this);
    this.seek = () => {
      this.pending?.abort();
      this.pending = null;
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
      this.failures = 0;
      this.retryAt = 0;
      this.hide();
    };
    container.appendChild(this.image);
    this.resize = new ResizeObserver(() => this.place());
    this.resize.observe(video);
    for (const event of EVENTS) video.addEventListener(event, this.update);
    video.addEventListener('seeking', this.seek);
    video.addEventListener('ended', this.seek);
    this.update();
  }

  place() {
    if (!this.cue || !this.video.videoWidth || !this.video.videoHeight) return;
    const video = this.video.getBoundingClientRect();
    const container = this.container.getBoundingClientRect();
    const scale = Math.min(video.width / this.video.videoWidth, video.height / this.video.videoHeight);
    const width = this.video.videoWidth * scale;
    const height = this.video.videoHeight * scale;
    const left = video.left - container.left + (video.width - width) / 2;
    const top = video.top - container.top + (video.height - height) / 2;
    const [x, y, w, h] = this.cue.box;
    this.image.style.left = (left + x * width) + 'px';
    this.image.style.top = (top + y * height) + 'px';
    this.image.style.width = (w * width) + 'px';
    this.image.style.height = (h * height) + 'px';
  }

  hide() {
    if (!this.cue && this.image.hidden) return;
    this.cue = null;
    this.image.hidden = true;
    this.image.removeAttribute('src');
  }

  update() {
    if (this.closed || this.video.seeking || this.video.ended) return;
    if (!this.video.paused && this.frame === null && this.video.requestVideoFrameCallback) {
      this.frame = this.video.requestVideoFrameCallback(() => {
        this.frame = null;
        this.update();
      });
    }
    const time = this.video.currentTime;
    const current = this.windows.find(window => window.start <= time && time < window.end);
    const cue = current?.cues.find(cue => cue.start <= time && time < cue.end);
    if (!cue) this.hide();
    else if (cue !== this.cue) {
      this.cue = cue;
      this.image.src = 'data:image/png;base64,' + cue.image;
      this.place();
      this.image.hidden = false;
    }
    if (this.pending || this.retryAt > performance.now()) return;
    const next = current ? current.end + .000001 : time;
    if (current && (current.end - time > 3 || this.windows.some(window => window.start <= next && next < window.end))) return;
    if (next >= this.video.duration - .000001) return;
    void this.load(next);
  }

  setStatus(state, message = '') {
    this.status = state;
    this.onStatus?.({ state, message, failures: this.failures });
  }

  retry() {
    if (this.closed) return;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.failures = 0;
    this.retryAt = 0;
    this.setStatus('loading', 'Retrying image subtitles…');
    this.update();
  }

  async load(position) {
    const url = new URL(this.url, window.location.origin);
    url.searchParams.set('position', position.toFixed(6));
    const controller = new AbortController();
    this.pending = controller;
    if (this.failures === 0 && this.windows.length === 0) {
      this.setStatus('loading', 'Loading image subtitles…');
    }
    try {
      const response = await fetch(url, {
        credentials: 'same-origin', redirect: 'error', cache: 'no-store',
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(22000)]),
      });
      if (!response.ok || Number(response.headers.get('content-length')) > MAX_WINDOW_BYTES) throw new Error('Image subtitle window unavailable');
      const data = await response.text();
      if (data.length > MAX_WINDOW_BYTES) throw new Error('Image subtitle window exceeded size limit');
      const window = JSON.parse(data);
      if (!validWindow(window, position)) throw new Error('Invalid image subtitle window');
      if (this.pending !== controller) return;
      this.windows = [...this.windows.filter(previous => previous.start !== window.start), window].slice(-2);
      this.failures = 0;
      this.retryAt = 0;
      this.setStatus('ready', 'Image subtitles ready.');
    } catch {
      if (this.pending === controller) {
        const index = Math.min(this.failures, BACKOFF_MS.length - 1);
        const delay = BACKOFF_MS[index];
        this.failures += 1;
        this.retryAt = performance.now() + delay;
        this.setStatus(
          this.failures >= BACKOFF_MS.length ? 'error' : 'retrying',
          this.failures >= BACKOFF_MS.length
            ? 'Image subtitles delayed. Retrying in background or seek to refresh.'
            : 'Retrying image subtitles…',
        );
        if (!this.closed) this.retryTimer = setTimeout(this.update, delay);
      }
    } finally {
      if (this.pending === controller) {
        this.pending = null;
        this.update();
      }
    }
  }

  destroy() {
    this.closed = true;
    this.seek();
    if (this.frame != null) this.video.cancelVideoFrameCallback(this.frame);
    for (const event of EVENTS) this.video.removeEventListener(event, this.update);
    this.video.removeEventListener('seeking', this.seek);
    this.video.removeEventListener('ended', this.seek);
    this.resize.disconnect();
    this.windows = [];
    this.image.remove();
  }
}
