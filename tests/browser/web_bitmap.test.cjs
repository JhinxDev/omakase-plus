'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function createHarness(fetchImpl) {
  let now = 1000;
  let nextTimerId = 0;
  const timers = new Map();
  const scheduledDelays = [];
  const timeoutValues = [];
  const statuses = [];
  const videoListeners = new Map();
  const video = {
    currentTime: 10,
    duration: 120,
    paused: true,
    seeking: false,
    ended: false,
    videoWidth: 1920,
    videoHeight: 1080,
    addEventListener(type, fn) {
      videoListeners.set(type, fn);
    },
    removeEventListener(type) {
      videoListeners.delete(type);
    },
    getBoundingClientRect() {
      return { left: 0, top: 0, width: 1280, height: 720 };
    },
  };
  const container = {
    children: [],
    appendChild(el) {
      this.children.push(el);
    },
    getBoundingClientRect() {
      return { left: 0, top: 0, width: 1280, height: 720 };
    },
  };
  const imageEl = {
    className: '',
    alt: '',
    hidden: true,
    style: {},
    setAttribute() {},
    removeAttribute(name) {
      if (name === 'src') delete this.src;
    },
    remove() {},
  };
  const source = fs
    .readFileSync(path.resolve(__dirname, '../../src/omakase/plus/static/web-bitmap.js'), 'utf8')
    .replace('export class BitmapSubtitles', 'class BitmapSubtitles');

  const context = {
    module: { exports: {} },
    document: { createElement: () => imageEl },
    window: { location: { origin: 'https://test.invalid' } },
    URL,
    performance: { now: () => now },
    AbortController,
    AbortSignal: {
      any: signals => signals[0],
      timeout: ms => {
        timeoutValues.push(ms);
        return new AbortController().signal;
      },
    },
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    setTimeout(fn, ms) {
      const id = ++nextTimerId;
      scheduledDelays.push(ms);
      timers.set(id, { fn, runAt: now + ms });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    fetch: fetchImpl,
  };
  vm.runInNewContext(source + '\nmodule.exports = { BitmapSubtitles };', context);
  const { BitmapSubtitles } = context.module.exports;

  const instance = new BitmapSubtitles({
    video,
    container,
    url: '/plus/cloud/media/0123456789abcdef0123456789abcdef/subtitles',
    onStatus: event => statuses.push(event),
  });

  async function flushMicrotasks() {
    for (let i = 0; i < 6; i++) await Promise.resolve();
  }

  async function advanceNextTimer() {
    const first = timers.entries().next().value;
    assert.ok(first, 'expected a scheduled retry timer');
    const [id, entry] = first;
    timers.delete(id);
    now = entry.runAt;
    entry.fn();
    await flushMicrotasks();
  }

  return {
    instance,
    video,
    statuses,
    scheduledDelays,
    timeoutValues,
    timers,
    flushMicrotasks,
    advanceNextTimer,
    setNow(value) {
      now = value;
    },
  };
}

test('BitmapSubtitles uses bounded exponential backoff, reports status, and recovers via retry', async () => {
  let attempt = 0;
  let shouldSucceed = false;
  const h = createHarness(async () => {
    attempt += 1;
    if (!shouldSucceed) {
      return { ok: false, status: 503, headers: new Headers() };
    }
    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-length': '200' }),
      async text() {
        return JSON.stringify({
          start: 0,
          end: 30,
          cues: [{ start: 5, end: 15, box: [0.1, 0.8, 0.8, 0.1], image: 'iVBORw0KGgoAAA' }],
        });
      },
    };
  });

  await h.flushMicrotasks();
  assert.equal(h.timeoutValues[0], 22000);
  assert.equal(attempt, 1);
  assert.equal(h.statuses.some(s => s.state === 'loading'), true);
  assert.equal(h.statuses.at(-1).state, 'retrying');
  assert.equal(Number.isFinite(h.instance.retryAt), true);

  // Trigger retries through [2000, 4000, 8000, 15000, 15000] without ever reaching Infinity.
  for (let i = 0; i < 4; i++) {
    await h.advanceNextTimer();
    assert.equal(Number.isFinite(h.instance.retryAt), true);
  }
  assert.deepEqual(h.scheduledDelays, [2000, 4000, 8000, 15000, 15000]);
  assert.equal(h.statuses.at(-1).state, 'error');

  // Calling retry() resets failure count and succeeds on the next response.
  shouldSucceed = true;
  h.instance.retry();
  await h.flushMicrotasks();
  assert.equal(h.instance.failures, 0);
  assert.equal(h.instance.status, 'ready');
  assert.equal(h.statuses.at(-1).state, 'ready');
  h.instance.destroy();
});
