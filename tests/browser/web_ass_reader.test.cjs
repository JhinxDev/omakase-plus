/* Actual cached ASS/font reader in a VM; no browser, WASM, provider or media job. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function reader(fetcher, beforeDelay = () => {}) {
  let now = 0, next = 0;
  const cancelled = new Set(), delays = [];
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/omakase/plus/static/web-ass.js'), 'utf8')
    .replace(/^import JASSUB from .*;$/m, 'const JASSUB = class {};')
    .replaceAll('import.meta.url', JSON.stringify('https://test.invalid/plus/static/web-ass.js'))
    .replace('export class OmakaseASS', 'class OmakaseASS');
  const context = {module: {exports: {}}, window: {}, location: new URL('https://test.invalid/plus/watch/1/1'), URL, Uint8Array, TextDecoder,
    AbortController, DOMException, fetch: fetcher, performance: {now: () => now},
    setTimeout: (fn, ms) => { const id = ++next; delays.push(ms); queueMicrotask(() => { beforeDelay(); if (!cancelled.has(id)) {now += ms; fn();} }); return id; },
    clearTimeout: id => cancelled.add(id)};
  vm.runInNewContext(source + '\nmodule.exports = {readBounded};', context);
  return {read: context.module.exports.readBounded, delays};
}
function response(status, retryAfter, cancelled, font = false) {
  const bytes = font ? new Uint8Array([0, 1, 0, 0, 1]) : new TextEncoder().encode('[Script Info]\n[Events]\n');
  return {ok: status === 200, status, headers: new Headers({'content-type': font ? 'font/ttf' : 'text/plain', ...(retryAfter ? {'retry-after': retryAfter} : {})}),
    body: status === 200 ? new ReadableStream({start(c) {c.enqueue(bytes);c.close();}}) : {cancel: async () => cancelled.push(status)}};
}

test('ASS GET retries 429 once after its typed backoff and cancels the rejected body', async () => {
  const requests = [], cancelled = [];
  const fixture = reader(async (url, options) => {requests.push([url, options]);return response(requests.length === 1 ? 429 : 200, '1', cancelled);});
  const bytes = await fixture.read('/cached.ass', 1024, 'ass', new AbortController().signal, 8000);
  assert.match(new TextDecoder().decode(bytes), /Script Info/);
  assert.equal(requests.length, 2);
  assert.deepEqual(fixture.delays, [1000]);
  assert.deepEqual(cancelled, [429]);
  for (const [url, options] of requests) {assert.equal(url, 'https://test.invalid/cached.ass');assert.equal(options.credentials, 'same-origin');assert.equal(options.redirect, 'error');}
});

test('only two extra GET attempts are allowed inside the same deadline', async () => {
  let count = 0;const cancelled = [];
  const fixture = reader(async () => {count++;return response(429, '1', cancelled);});
  await assert.rejects(fixture.read('/cached.ass', 1024, 'ass', new AbortController().signal, 8000), /429/);
  assert.equal(count, 3);assert.deepEqual(fixture.delays, [1000, 1000]);assert.equal(cancelled.length, 3);
});

test('rate backoff beyond remaining deadline and all non429 failures are never retried', async () => {
  for (const [status, retry] of [[429, '10'], [429, '0'], [429, '-1'], [429, 'bad'], [401, '1'], [403, '1'], [404, '1'], [409, '1'], [500, '1']]) {
    let count = 0;const cancelled = [];
    const fixture = reader(async () => {count++;return response(status, retry, cancelled);});
    await assert.rejects(fixture.read('/cached.ass', 1024, 'ass', new AbortController().signal, 8000));
    assert.equal(count, 1);assert.deepEqual(fixture.delays, []);
    if (status === 429) assert.deepEqual(cancelled, [429]);
  }
});

test('abort during the retry wait cancels the wait and never issues another GET', async () => {
  let count = 0;const abort = new AbortController();
  const fixture = reader(async () => {count++;return response(429, '1', []);}, () => abort.abort());
  await assert.rejects(fixture.read('/cached.ass', 1024, 'ass', abort.signal, 8000), error => error.name === 'AbortError');
  assert.equal(count, 1);
});

test('font GET uses the same bounded retry and retains signature validation', async () => {
  let count = 0;const cancelled = [];
  const fixture = reader(async () => response(++count === 1 ? 429 : 200, '1', cancelled, true));
  assert.equal((await fixture.read('/font.bin', 1024, 'font', new AbortController().signal, 8000)).length, 5);
  assert.equal(count, 2);assert.deepEqual(cancelled, [429]);assert.deepEqual(fixture.delays, [1000]);
  const invalid = reader(async () => {const result = response(200, null, []);result.headers.set('content-type', 'application/octet-stream');return result;});
  await assert.rejects(invalid.read('/font.bin', 1024, 'font', new AbortController().signal, 8000), /signature/);
});

test('ASS and later font requests share the original absolute load deadline', async () => {
  let count = 0;
  const fixture = reader(async () => response(++count === 2 ? 200 : 429, '7', []));
  const signal = new AbortController().signal;
  await fixture.read('/cached.ass', 1024, 'ass', signal, 8000);
  await assert.rejects(fixture.read('/font.bin', 1024, 'font', signal, 8000), /429/);
  assert.equal(count, 3);assert.deepEqual(fixture.delays, [7000]);
});

test('network errors and an already cancelled load never trigger a retry', async () => {
  let count = 0;
  const fixture = reader(async () => {count++;throw new TypeError('network');});
  await assert.rejects(fixture.read('/cached.ass', 1024, 'ass', new AbortController().signal, 8000), /network/);
  assert.equal(count, 1);
  const abort = new AbortController();abort.abort();
  await assert.rejects(fixture.read('/cached.ass', 1024, 'ass', abort.signal, 8000), error => error.name === 'AbortError');
  assert.equal(count, 1);
});
