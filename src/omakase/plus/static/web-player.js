/* Omakase playback uses the supplied Shaka UI. All state writes are serialized. */
(() => {
  'use strict';
  class EventQueue {
    constructor(send, onError, onSaved = () => {}, minimumGapMs = 0) {
      this.send = send;
      this.onError = onError;
      this.onSaved = onSaved;
      this.pending = [];
      this.running = false;
      this.stopped = false;
      this.seq = 0;
      this.waiters = [];
      this.minimumGapMs = minimumGapMs;
      this.lastAcknowledged = 0;
      this.departing = false;
      this.latestIntent = 0;
      this.waiting = null;
      this.acknowledgedIntent = 0;
      this.lastSeekIntent = 0;
      this.sealedPayload = null;
    }
    push(event, keepalive = false, sampleBeforeSend = null) {
      if (this.stopped) return;
      const intent = ++this.latestIntent;
      if (event.event === 'seek') this.lastSeekIntent = intent;
      const pausedSeek = item => item?.event.event === 'seek' && item.event.playing === false;
      if (event.event === 'seek' && event.playing === false) {
        // Unsent paused seeks carry no played interval. Keep the newest target
        // while retaining every play/pause boundary and all dispatched payloads.
        if (pausedSeek(this.waiting) && this.pending.every(pausedSeek)) {
          Object.assign(this.waiting, { event: { ...event }, keepalive, sampleBeforeSend, intent });
          this.pending = [];
          return;
        }
        const last = this.pending[this.pending.length - 1];
        if (pausedSeek(last)) {
          Object.assign(last, { event: { ...event }, keepalive, sampleBeforeSend, intent });
          return;
        }
      }
      const last = this.pending[this.pending.length - 1];
      if (event.event === 'progress' && last?.event.event === 'progress') {
        last.event = event;
        last.sampleBeforeSend = sampleBeforeSend;
        last.intent = intent;
        return;
      }
      // Never silently discard a seek or terminal state under a stalled network.
      if (this.pending.length >= 12) {
        this.stopped = true;
        this.pending = [];
        this.onError(new Error('Progress could not keep up. Reload the player before continuing.'));
        return;
      }
      this.pending.push({ event: { ...event }, keepalive, sampleBeforeSend, intent });
      void this.drain();
    }
    async drain() {
      if (this.running) return;
      this.running = true;
      while (this.pending.length && !this.stopped) {
        const item = this.pending.shift();
        this.waiting = item;
        try {
          const wait = this.minimumGapMs - (Date.now() - this.lastAcknowledged);
          if (wait > 0) await new Promise(resolve => setTimeout(resolve, wait));
          if (this.stopped) break;
          this.waiting = null;
          // The server anchors elapsed time at receipt. Sample an uninterrupted
          // live clock after pacing, never while waiting in the queue. Freeze
          // this exact payload before the first attempt and its one retry.
          const payload = { ...(item.sampleBeforeSend?.() || item.event), seq: ++this.seq };
          let result;
          try { result = await this.send(payload, item.keepalive || this.departing); }
          catch (error) {
            if (error.status || item.keepalive || this.departing) throw error;
            // One bounded transport retry, with the exact same idempotency sequence.
            result = await this.send(payload, item.keepalive);
          }
          this.lastAcknowledged = Date.now();
          this.acknowledgedIntent = item.intent;
          if (!this.stopped) this.onSaved(result, { latest: item.intent === this.latestIntent, intent: item.intent });
        } catch (error) {
          const alreadyStopped = this.stopped;
          this.stopped = true;
          this.pending = [];
          if (!alreadyStopped) this.onError(error);
        }
      }
      this.running = false;
      this.waiting = null;
      this.waiters.splice(0).forEach(resolve => resolve());
    }
    idle() { return this.running ? new Promise(resolve => this.waiters.push(resolve)) : Promise.resolve(); }
    seal(finalEvent) {
      if (this.sealedPayload) return this.sealedPayload;
      if (!['seek', 'pause', 'ended'].includes(finalEvent.event) || finalEvent.playing !== false || this.seq >= 1000000) throw new Error('Invalid terminal playback event');
      this.stopped = true;
      this.departing = true;
      this.pending = [];
      this.waiting = null;
      this.sealedPayload = Object.freeze({ ...finalEvent, seq: ++this.seq });
      return this.sealedPayload;
    }
    hasPendingSeek() { return this.lastSeekIntent > this.acknowledgedIntent; }
  }
  function selectedTracks(player) {
    const selectedAudio = player?.getAudioTracks().find(track => track.active);
    // Shaka 5 reports Off through no active text track. Its removed
    // isTextTrackVisible API must not be called during the first progress save.
    const selectedText = player?.getTextTracks().find(track => track.active);
    const variant = player?.getVariantTracks?.().find(track => track.active);
    const audioId = selectedAudio?.id ?? variant?.audioId;
    return { audio_id: audioId != null ? String(audioId) : null, text_id: selectedText ? String(selectedText.id) : null };
  }
  function browserCapabilities(scope, video) {
    const engines = [scope.MediaSource, scope.ManagedMediaSource].filter(engine => typeof engine?.isTypeSupported === 'function');
    const types = {
      h264: ['video/mp4; codecs="avc1.640028"', 'video/mp4; codecs="avc1.42E01E"'],
      hevc: ['video/mp4; codecs="hvc1.1.6.L120.B0"', 'video/mp4; codecs="hvc1.2.4.L153.B0"'],
      av1: ['video/mp4; codecs="av01.0.08M.08"'], aac: ['audio/mp4; codecs="mp4a.40.2"'],
      opus: ['audio/mp4; codecs="opus"'], ac3: ['audio/mp4; codecs="ac-3"'], eac3: ['audio/mp4; codecs="ec-3"'],
    };
    const codecs = Object.keys(types).filter(codec => engines.some(engine => types[codec].some(type => {
      try { return engine.isTypeSupported(type) === true; } catch { return false; }
    })));
    const ass_supported = scope.crossOriginIsolated === true && typeof scope.SharedArrayBuffer === 'function'
      && typeof scope.OffscreenCanvas === 'function' && typeof scope.Worker === 'function'
      && typeof video.requestVideoFrameCallback === 'function';
    return { codecs, ass_supported };
  }
  function subtitlePreference(subtitles, savedId, versionChanged = false) {
    if (!versionChanged && savedId === 'off') return 'off';
    if (!versionChanged && subtitles.some(track => `ass:${track.plex_stream_id}` === savedId)) return savedId;
    const preferred = subtitles.find(track => track.selected) || subtitles.find(track => track.forced)
      || subtitles.find(track => track.default) || subtitles.find(track => /^(en|eng)(-|$)/i.test(track.language || '')) || subtitles[0];
    return preferred ? `ass:${preferred.plex_stream_id}` : 'off';
  }
  function audioPreference(player, state) {
    const audio = player.getAudioTracks();
    const variants = player.getVariantTracks?.() || [];
    const savedId = !state.resume_version_changed ? state.audio_id : null;
    if (savedId != null) {
      const native = audio.filter(track => track.id != null && String(track.id) === String(savedId));
      if (native.length === 1) return { native: native[0] };
    }
    let candidates = savedId != null ? variants.filter(track => track.audioId != null && String(track.audioId) === String(savedId)) : [];
    if (!candidates.length) {
      const selected = (state.track_map || []).filter(track => track.kind === 'audio' && track.selected && track.representation_id != null);
      if (selected.length !== 1) return null;
      candidates = variants.filter(track => track.originalAudioId === String(selected[0].representation_id));
    }
    const current = variants.find(track => track.active);
    const sameVideo = candidates.filter(track => track.videoId === current?.videoId);
    if (sameVideo.length === 1) return { variant: sameVideo[0] };
    return candidates.length === 1 ? { variant: candidates[0] } : null;
  }
  function eventPosition(currentTime, serverDuration) {
    if (!Number.isFinite(currentTime) || !Number.isFinite(serverDuration) || serverDuration <= 0) return null;
    // Mux rounding can extend the native media clock; the server timeline owns resume.
    return Math.min(Math.max(0, currentTime), serverDuration);
  }
  function playbackRateConfiguration() {
    return { playbackRates: [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2], playbackRateSliderMin: 0.25, playbackRateSliderMax: 2, playbackRateSliderStep: 0.05 };
  }
  function styledSubtitleTracks(subtitles) {
    return subtitles.filter(track => ['ass', 'ssa'].includes(String(track.codec).toLowerCase()) && track.plex_stream_id != null);
  }
  class SessionPreparation {
    constructor(request, pending, onPending = () => {}) {
      this.request = request;
      this.pending = pending;
      this.onPending = onPending;
      this.controller = new AbortController();
      this.cancelled = false;
      this.closePromise = null;
    }
    async resolve() {
      const session = this.pending;
      if (!session.preparation_required) return session;
      const expected = `/plus/api/playback/sessions/${encodeURIComponent(session.session_id)}/prepare`;
      if (session.prepare_url !== expected) throw new Error('Invalid preparation endpoint');
      if (this.cancelled) throw new DOMException('Preparation cancelled', 'AbortError');
      this.onPending(session);
      // Preparation is one explicit operation. No automatic job retry.
      const prepared = await this.request(expected, {}, false, 55000, this.controller.signal);
      if (this.cancelled) throw new DOMException('Preparation cancelled', 'AbortError');
      if (prepared.session_id !== session.session_id || prepared.preparation_required) throw new Error('Invalid prepared session');
      return prepared;
    }
    cancel(keepalive = false) {
      this.cancelled = true;
      this.controller.abort();
      if (!this.closePromise) {
        const path = `/plus/api/playback/sessions/${encodeURIComponent(this.pending.session_id)}/close`;
        this.closePromise = this.request(path, {}, keepalive).catch(error => {
          // The prepare endpoint can already have closed its failed/disconnected
          // session before the explicit close reaches it.
          if (![404, 410].includes(error.status)) throw error;
        });
      }
      return this.closePromise;
    }
  }
  function startPayload(config, capabilities) {
    return { monitor_id: config.monitor_id, episode_number: config.episode_number, library_id: config.library_id, ...capabilities, defer_prepare: true };
  }
  function exitSnapshot(snapshot, queue, seeking = false, ended = false) {
    return { ...snapshot, event: seeking || queue.hasPendingSeek() ? 'seek' : ended ? 'ended' : 'pause', playing: false };
  }
  class TerminalCheckpoint {
    constructor(storage, now = () => Date.now()) {
      this.storage = storage;
      this.now = now;
      this.key = 'omakase.pendingTerminal.v1';
    }
    valid(record) {
      const event = record?.final_event;
      return record && Object.keys(record).every(key => ['session_id', 'final_event', 'expires_at'].includes(key))
        && typeof record.session_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(record.session_id)
        && Number.isFinite(record.expires_at) && record.expires_at > this.now() && record.expires_at <= this.now() + 60000
        && event && Number.isInteger(event.seq) && event.seq > 0 && event.seq <= 1000000
        && Object.keys(event).every(key => ['seq', 'event', 'position_seconds', 'playing', 'playback_rate', 'audio_id', 'text_id'].includes(key))
        && ['seek', 'pause', 'ended'].includes(event.event) && event.playing === false
        && Number.isFinite(event.position_seconds) && event.position_seconds >= 0 && event.position_seconds <= 43200
        && Number.isFinite(event.playback_rate) && event.playback_rate >= .25 && event.playback_rate <= 2
        && [event.audio_id, event.text_id].every(id => id == null || typeof id === 'string' && id.length <= 128 && !/[\x00-\x1f]/.test(id));
    }
    read() {
      if (!this.storage) return { unconfirmed: true };
      try {
        const raw = this.storage.getItem(this.key);
        if (!raw) return { unconfirmed: false };
        const record = raw.length <= 4096 ? JSON.parse(raw) : null;
        if (this.valid(record)) return { record, unconfirmed: false };
        this.storage.removeItem(this.key);
      } catch { /* Unavailable or expired storage cannot establish a saved exit. */ }
      return { unconfirmed: true };
    }
    write(session_id, final_event) {
      const record = { session_id, final_event, expires_at: this.now() + 60000 };
      const raw = JSON.stringify(record);
      if (!this.storage || !this.valid(record) || new TextEncoder().encode(raw).length > 4096) return null;
      try { this.storage.setItem(this.key, raw); return record; } catch { return null; }
    }
    clear(record) {
      if (!record || !this.storage) return;
      try {
        const current = this.storage.getItem(this.key);
        if (current === JSON.stringify(record)) this.storage.removeItem(this.key);
      } catch { /* A confirmed receipt remains safe to replay until its short expiry. */ }
    }
    async flush(request) {
      const state = this.read();
      if (state.record) {
        const record = state.record;
        await request(`/plus/api/playback/sessions/${encodeURIComponent(record.session_id)}/close`, { final_event: record.final_event }, false, 10000, null, 204);
        this.clear(record);
      }
      return state;
    }
  }
  function enforcePlayback(video, state) {
    if (state.ready && !state.failed && !state.leaving && !state.captionLoading) return true;
    video.pause();
    return false;
  }
  function guardedPlay(video, nativePlay, state) {
    return (...args) => enforcePlayback(video, state()) ? nativePlay(...args) : Promise.resolve();
  }
  const enhancedRanges = new WeakSet();
  function enhanceRangeControls(container) {
    container.querySelectorAll('input.shaka-range-element[type="range"]').forEach(range => {
      const speed = range.classList.contains('shaka-playback-rate-slider');
      if (speed || (!range.getAttribute('aria-label') && !range.getAttribute('aria-labelledby'))) {
        range.setAttribute('aria-label', speed ? 'Playback speed' : range.classList.contains('shaka-seek-bar') ? 'Playback position' : 'Volume');
      }
      if (enhancedRanges.has(range)) return;
      enhancedRanges.add(range);
      // Speed/volume use native range input events. Keep Shaka's seek-specific
      // keyboard lifecycle, which starts/commits its delayed seek interaction.
      // Its general Home/End shortcuts must not reach other focused ranges.
      const keepRangeNavigation = event => {
        if (!range.classList.contains('shaka-seek-bar') && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) event.stopPropagation();
      };
      range.addEventListener('keydown', keepRangeNavigation);
      range.addEventListener('keyup', keepRangeNavigation);
      if (speed) {
        const describe = () => range.setAttribute('aria-valuetext', `${Number(range.value)} times normal speed`);
        range.addEventListener('input', describe);
        describe();
      }
    });
    const steps = container.querySelectorAll('.shaka-playback-rate-step-btn');
    steps.forEach(button => button.setAttribute('aria-label', button.textContent.trim() === '+' ? 'Increase playback speed' : 'Decrease playback speed'));
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { EventQueue, selectedTracks, browserCapabilities, subtitlePreference, audioPreference, eventPosition, playbackRateConfiguration, styledSubtitleTracks, enhanceRangeControls, SessionPreparation, startPayload, exitSnapshot, TerminalCheckpoint, enforcePlayback, guardedPlay };
  if (typeof document === 'undefined') return;
  const root = document.querySelector('[data-web-player]');
  if (!root) return;
  const find = name => root.querySelector(`[data-player-${name}]`);
  const config = JSON.parse(document.getElementById('web-player-config').textContent);
  const video = find('video');
  const container = find('container');
  const status = find('status');
  const saved = find('save');
  const start = find('start');
  const restart = find('restart');
  const retry = find('retry');
  const reload = find('reload');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  let session = null;
  let player = null;
  let overlay = null;
  let queue = null;
  let ready = false;
  let starting = false;
  let leaving = false;
  let triedAgain = false;
  let failed = false;
  let periodic = null;
  let captions = null;
  let captionTasks = Promise.resolve();
  let captionIntent = 0;
  let assTracks = [];
  let customTextId = null;
  let captionLoading = false;
  let samplingEpoch = 0;
  let preparation = null;
  let attempt = 0;
  let priorExitUnconfirmed = false;
  let tabStorage = null;
  try { tabStorage = window.sessionStorage; } catch { /* Exit saving remains best effort without tab storage. */ }
  const terminalCheckpoint = new TerminalCheckpoint(tabStorage);
  const playbackState = () => ({ ready, failed, leaving, captionLoading });
  // Supplied controls call this instance method. Gate before native play so a
  // terminal error cannot restart buffered media or create an unhandled aborted
  // play promise. Native browser controls are also guarded by the events below.
  video.play = guardedPlay(video, video.play.bind(video), playbackState);

  async function post(path, body, keepalive = false, timeoutMs = 10000, externalSignal = null, expectedStatus = null) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (externalSignal?.aborted) abort();
    externalSignal?.addEventListener('abort', abort, { once: true });
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {
        method: 'POST', credentials: 'same-origin', keepalive, signal: controller.signal,
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf || '', Accept: 'application/json' },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const error = new Error('Playback request failed');
        error.status = response.status;
        const body = await response.json().catch(() => null);
        error.code = body?.code;
        error.source = body?.source;
        throw error;
      }
      if (expectedStatus !== null && response.status !== expectedStatus) throw new Error('Playback save acknowledgement unavailable');
      return response.status === 204 ? {} : await response.json();
    } finally { window.clearTimeout(timeout); externalSignal?.removeEventListener('abort', abort); }
  }
  function sessionPath(suffix = '') { return `/plus/api/playback/sessions/${encodeURIComponent(session.session_id)}${suffix}`; }
  function showError(error, saving = false) {
    failed = true;
    // Stop event capture before pause, so a failing save cannot enqueue another write.
    ready = false;
    video.pause();
    void stopCaptions().catch(() => {});
    window.clearInterval(periodic);
    const blocked = [401, 403, 409].includes(error.status);
    find('error').hidden = false;
    find('error-text').textContent = blocked
      ? 'Your session or library access changed. Reload this page before continuing.'
      : saving ? 'Playback paused because your progress could not be saved. Reload to continue with your last confirmed save.'
        : error.code === 'ass_display' ? 'Styled subtitles cannot display in this playback mode. Use the player’s fullscreen control, turn subtitles Off, or open this episode in Plex.'
        : 'This episode could not play in this browser. You can try once more, or use Plex for other formats and subtitle support.';
    if (safeLink(find('plex'), error.source?.plex_url)) find('fallback').hidden = true;
    status.textContent = 'Playback paused.';
    reload.hidden = !(blocked || saving);
    retry.hidden = blocked || saving || triedAgain;
    restart.disabled = true;
    start.disabled = true;
    start.hidden = false;
    find('resume-option').hidden = false;
    if (saving) saved.textContent = 'Progress save interrupted.';
  }
  function tracks() {
    // These are Shaka track IDs for this media version, never Plex stream IDs.
    return { ...selectedTracks(player), ...(assTracks.length ? { text_id: customTextId || 'off' } : {}) };
  }
  function playbackSnapshot(event) {
    const position = eventPosition(video.currentTime, session?.duration_seconds);
    if (position === null) return null;
    return {
      event, position_seconds: position,
      playing: !['pause', 'ended'].includes(event) && !video.paused && !video.ended && !video.seeking && video.readyState >= 3,
      playback_rate: video.playbackRate, ...tracks(),
    };
  }
  function capture(event, keepalive = false, resample = true) {
    if (!ready || !queue) return;
    if (event !== 'progress' || !resample) samplingEpoch++;
    const epoch = samplingEpoch;
    const source = session;
    const snapshot = playbackSnapshot(event);
    if (!snapshot) return;
    saved.textContent = 'Saving progress…';
    const sample = resample && ['play', 'progress'].includes(event) ? () => {
      if (epoch !== samplingEpoch || source !== session || !ready || video.seeking || video.paused !== !snapshot.playing) return null;
      return playbackSnapshot(event);
    } : null;
    queue.push(snapshot, keepalive, sample);
  }
  function safeLink(node, value, localOnly = false) {
    if (typeof value !== 'string' || !value) return false;
    try {
      const url = new URL(value, window.location.origin);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || (localOnly && url.origin !== window.location.origin)) return false;
      node.href = url.href;
      node.hidden = false;
      return true;
    } catch { return false; }
  }
  function configureControls() {
    if (!overlay) return;
    const styledActive = assTracks.length > 0 && customTextId !== 'off';
    video.disablePictureInPicture = styledActive;
    overlay.configure({
      ...playbackRateConfiguration(),
      controlPanelElements: ['play_pause', 'time_and_duration', 'spacer', 'mute', 'volume', 'fullscreen', 'overflow_menu'],
      overflowMenuButtons: ['quality', 'language', ...(!assTracks.length ? ['captions'] : []), 'playback_rate', ...(!styledActive ? ['picture_in_picture'] : [])],
      seekBarColors: { base: '#626577', buffered: '#a6a3b3', played: '#d4c5f7' },
    });
    enhanceRangeControls(container);
    find('caption-mode-note').hidden = !styledActive;
  }
  async function stopCaptions() {
    captionIntent++;
    const old = captions;
    captions = null;
    captionLoading = false;
    captionTasks = captionTasks.catch(() => {}).then(async () => { if (old) await old.destroy(); });
    await captionTasks;
  }
  function changeCaption(id, resumePlayback = false) {
    const intent = ++captionIntent;
    const source = session;
    video.pause();
    captionLoading = true;
    find('ass-status').textContent = id === 'off' ? 'Turning styled subtitles off…' : 'Preparing styled subtitles…';
    const task = captionTasks.catch(() => {}).then(async () => {
      if (intent !== captionIntent || source !== session || leaving) return;
      try {
        if (id === 'off') {
          if (captions) await captions.clear();
        } else {
          const track = assTracks.find(item => `ass:${item.plex_stream_id}` === id);
          if (!track || !browserCapabilities(window, video).ass_supported) throw Object.assign(new Error('Styled subtitles unavailable'), { code: 'ass_display' });
          if (!captions) {
            const { OmakaseASS } = await import('/plus/static/web-ass.js');
            if (intent !== captionIntent || source !== session || leaving) return;
            const controller = new OmakaseASS({ video, container, onError: () => {
              if (captions === controller && source === session && !leaving) showError(Object.assign(new Error('Styled subtitles failed'), { code: 'ass_display' }));
            } });
            captions = controller;
          }
          const success = await captions.setTrack({ url: track.url, fonts: source.fonts || [], timeOffset: 0 });
          if (!success) return;
        }
        if (intent !== captionIntent || source !== session || leaving) return;
        customTextId = id;
        find('ass-select').value = id;
        find('ass-status').textContent = id === 'off' ? 'Styled subtitles are off.' : 'Styled subtitles ready.';
        captionLoading = false;
        configureControls();
        capture('progress');
        if (resumePlayback && ready && !failed) {
          try { await video.play(); } catch { status.textContent = 'Subtitles ready. Press Play to continue.'; }
        }
      } catch (error) {
        if (intent === captionIntent && source === session && !leaving) showError(Object.assign(error, { code: 'ass_display' }));
        throw error;
      } finally { if (intent === captionIntent) captionLoading = false; }
    });
    captionTasks = task;
    return task;
  }
  async function prepareTracks() {
    assTracks = styledSubtitleTracks(session.subtitles || []);
    customTextId = null;
    find('ass-options').hidden = !assTracks.length;
    const selector = find('ass-select');
    selector.disabled = true;
    selector.replaceChildren(new Option('Off', 'off'));
    assTracks.forEach((track, index) => {
      const language = track.language && track.language !== 'und' ? track.language : 'Language unknown';
      const labels = [language, 'Styled ASS', ...(track.forced ? ['Forced'] : []), ...(track.default ? ['Default'] : [])];
      selector.add(new Option(`${labels.join(' · ')} (${index + 1})`, `ass:${track.plex_stream_id}`));
    });
    const audio = audioPreference(player, session);
    if (audio?.native) player.selectAudioTrack(audio.native);
    else if (audio?.variant) player.selectVariantTrack(audio.variant, true);
    if (assTracks.length) {
      player.selectTextTrack(null);
      const initial = subtitlePreference(assTracks, session.text_id, session.resume_version_changed);
      customTextId = initial;
      configureControls();
      await changeCaption(initial);
    } else {
      const text = !session.resume_version_changed && session.text_id != null
        ? player.getTextTracks().find(track => String(track.id) === session.text_id) : null;
      if (session.text_id === 'off') player.selectTextTrack(null);
      else if (text) player.selectTextTrack(text);
      configureControls();
    }
  }
  async function dispose() {
    ready = false;
    window.clearInterval(periodic);
    video.pause();
    if (preparation) {
      const pending = preparation;
      preparation = null;
      try { await pending.cancel(leaving); } catch { /* Server expiry/disconnect still stops the pending operation. */ }
      if (session?.session_id === pending.pending.session_id) session = null;
    }
    await stopCaptions();
    if (queue) await queue.idle();
    if (session) {
      try { await post(sessionPath('/close'), {}, leaving); } catch { /* Expiring server sessions provide the fallback. */ }
    }
    session = null;
    assTracks = [];
    customTextId = null;
    video.disablePictureInPicture = false;
    if (overlay) await overlay.destroy();
    if (player) await player.destroy();
    overlay = null;
    player = null;
  }
  async function begin() {
    if (starting || leaving) return;
    starting = true;
    const currentAttempt = ++attempt;
    failed = false;
    start.disabled = true;
    start.hidden = false;
    find('resume-option').hidden = false;
    find('cancel-preparation').hidden = true;
    find('start-panel').hidden = false;
    find('start-heading').textContent = 'Opening your episode';
    find('start-copy').textContent = 'Checking this episode’s available streams.';
    retry.hidden = true;
    find('error').hidden = true;
    status.textContent = 'Preparing your episode…';
    container.setAttribute('aria-busy', 'true');
    try {
      if (!csrf) throw new Error('Missing session protection');
      if (!window.shaka?.ui?.Overlay) throw new Error('Player unavailable');
      shaka.polyfill.installAll();
      if (!shaka.Player.isBrowserSupported()) throw new Error('Browser unsupported');
      await dispose();
      if (leaving) return;
      status.textContent = 'Confirming your last saved position…';
      let priorExit;
      try { priorExit = await terminalCheckpoint.flush(post); }
      catch (error) { priorExitUnconfirmed = true; saved.textContent = 'Previous exit save was not confirmed.'; throw error; }
      priorExitUnconfirmed = priorExit.record ? false : priorExitUnconfirmed || priorExit.unconfirmed;
      if (priorExitUnconfirmed) saved.textContent = 'Previous exit save could not be verified.';
      if (currentAttempt !== attempt || leaving) return;
      status.textContent = 'Opening your episode…';
      session = await post('/plus/api/playback/sessions', startPayload(config, browserCapabilities(window, video)), false, 40000);
      if (!session || typeof session.session_id !== 'string' || !session.session_id) throw new Error('Invalid playback session');
      if (!Number.isFinite(session.duration_seconds) || session.duration_seconds <= 0) throw new Error('Invalid playback timeline');
      if (safeLink(find('plex'), session.source?.plex_url)) find('fallback').hidden = true;
      if (session.preparation_required) {
        const pending = new SessionPreparation(post, session, () => {
          find('start-heading').textContent = 'Preparing your episode';
          find('start-copy').textContent = 'Preparing this episode. This can take up to 45 seconds.';
          status.textContent = 'Preparing this episode. This can take up to 45 seconds.';
          find('resume').disabled = true;
          find('resume-option').hidden = true;
          start.hidden = true;
          find('cancel-preparation').hidden = false;
          find('cancel-preparation').disabled = false;
        });
        preparation = pending;
        const prepared = await pending.resolve();
        if (currentAttempt !== attempt || leaving) return;
        preparation = null;
        session = prepared;
        find('cancel-preparation').hidden = true;
        find('resume').disabled = false;
        find('resume-option').hidden = false;
        if (safeLink(find('plex'), session.source?.plex_url)) find('fallback').hidden = true;
      }
      if (currentAttempt !== attempt || leaving) return;
      safeLink(find('next'), session.next_url, true);
      const media = new URL(session.media_url, window.location.origin);
      if (media.origin !== window.location.origin || !['http:', 'https:'].includes(media.protocol)) throw new Error('Invalid media source');
      player = new shaka.Player();
      await player.attach(video);
      overlay = new shaka.ui.Overlay(player, container, video);
      configureControls();
      // A manifest cannot make the browser contact an unrelated origin.
      player.getNetworkingEngine().registerRequestFilter((_type, request) => {
        if (request.uris.some(uri => new URL(uri, media).origin !== window.location.origin)) throw new Error('External playback request blocked');
      });
      player.addEventListener('error', () => { if (!leaving) showError(new Error('Unsupported stream')); });
      const position = find('resume').checked && !session.resume_version_changed ? Math.max(0, Number(session.position_seconds) || 0) : 0;
      find('version-note').hidden = !session.resume_version_changed;
      video.preload = 'metadata';
      await player.load(media.href, position, session.media_type || undefined);
      if (failed || leaving) return;
      await prepareTracks();
      if (failed || leaving) return;
      queue = new EventQueue(
        (payload, keepalive) => post(sessionPath('/events'), payload, keepalive),
        error => showError(error, true),
        (_result, state) => { if (!failed && state.latest) saved.textContent = priorExitUnconfirmed ? 'Current position saved. Previous exit unverified.' : 'Progress saved.'; },
        275,
      );
      ready = true;
      find('ass-select').disabled = false;
      player.addEventListener('variantchanged', () => capture('progress'));
      player.addEventListener('textchanged', () => capture('progress'));
      restart.disabled = false;
      find('start-panel').hidden = true;
      status.textContent = session.source?.mode === 'direct' ? 'Ready to play · Direct stream' : 'Ready to play';
      capture('seek');
      periodic = window.setInterval(() => { if (!video.paused && !video.ended) capture('progress'); }, 5000);
      try { await video.play(); }
      catch { status.textContent = 'Ready. Press Play in the player controls to continue.'; }
    } catch (error) { if (currentAttempt === attempt && !leaving) showError(error); }
    finally {
      if (currentAttempt === attempt) {
        starting = false;
        start.hidden = false;
        find('resume-option').hidden = false;
        find('cancel-preparation').hidden = true;
        find('resume').disabled = false;
        container.removeAttribute('aria-busy');
      }
    }
  }
  find('cancel-preparation').addEventListener('click', async () => {
    if (!preparation) return;
    const pending = preparation;
    const cancellation = ++attempt;
    find('cancel-preparation').disabled = true;
    status.textContent = 'Cancelling preparation…';
    try {
      await pending.cancel();
      if (cancellation !== attempt || leaving) return;
      preparation = null;
      if (session?.session_id === pending.pending.session_id) session = null;
      starting = false;
      start.disabled = false;
      start.hidden = false;
      find('resume-option').hidden = false;
      find('cancel-preparation').hidden = true;
      find('resume').disabled = false;
      container.removeAttribute('aria-busy');
      find('start-heading').textContent = 'Preparation cancelled';
      find('start-copy').textContent = 'Choose Play episode to try again, or open this episode in Plex.';
      status.textContent = 'Preparation cancelled. Playback has not started.';
      start.focus();
    } catch (error) {
      if (cancellation === attempt && !leaving) {
        starting = false;
        start.hidden = false;
        find('resume-option').hidden = false;
        find('cancel-preparation').hidden = true;
        container.removeAttribute('aria-busy');
        showError(error);
        reload.hidden = false;
      }
    }
  });
  start.addEventListener('click', () => void begin());
  retry.addEventListener('click', () => { if (!triedAgain) { triedAgain = true; void begin(); } });
  reload.addEventListener('click', () => window.location.reload());
  find('ass-select').addEventListener('change', event => {
    const wasPlaying = !video.paused;
    void changeCaption(event.target.value, wasPlaying).catch(() => {});
  });
  restart.addEventListener('click', () => { if (ready) { video.currentTime = 0; status.textContent = 'Restarted from the beginning.'; } });
  video.addEventListener('playing', () => {
    if (!enforcePlayback(video, playbackState())) return;
    status.textContent = 'Playing'; capture('play');
  });
  video.addEventListener('play', () => { enforcePlayback(video, playbackState()); });
  function unsupportedCaptionMode() {
    if (assTracks.length && customTextId !== 'off') showError(Object.assign(new Error('Canvas subtitles unavailable in native video mode'), { code: 'ass_display' }));
  }
  video.addEventListener('webkitbeginfullscreen', unsupportedCaptionMode);
  video.addEventListener('enterpictureinpicture', () => {
    if (assTracks.length && customTextId !== 'off') {
      if (document.pictureInPictureElement) void document.exitPictureInPicture().catch(() => {});
      unsupportedCaptionMode();
    }
  });
  video.addEventListener('pause', () => { if (ready && !leaving && !video.ended) { status.textContent = 'Paused'; capture('pause'); } });
  video.addEventListener('waiting', () => { if (ready) { status.textContent = 'Buffering…'; capture('pause'); } });
  video.addEventListener('seeking', () => capture('seek'));
  video.addEventListener('seeked', () => capture(video.paused ? 'seek' : 'play'));
  video.addEventListener('ratechange', () => {
    capture('progress', false, false);
    // Preset buttons and shortcuts also update the slider without native input.
    container.querySelectorAll('.shaka-playback-rate-slider').forEach(range => range.setAttribute('aria-valuetext', `${video.playbackRate} times normal speed`));
  });
  video.addEventListener('ended', () => { if (ready) { status.textContent = 'Episode finished. Your viewing progress is being saved.'; capture('ended'); } });
  // Navigation remains manual; the next-episode link never starts playback itself.
  window.addEventListener('pagehide', () => {
    if (leaving) return;
    leaving = true;
    if (ready && queue && session) {
      const snapshot = playbackSnapshot('pause');
      if (snapshot) {
        // A single immediate request owns the paced terminal save on the server;
        // no document timer or queued callback is required after navigation.
        const final_event = queue.seal(exitSnapshot(snapshot, queue, video.seeking, video.ended));
        const closePath = sessionPath('/close');
        const checkpoint = terminalCheckpoint.write(session.session_id, final_event);
        ready = false;
        session = null;
        saved.textContent = 'Saving progress…';
        void post(closePath, { final_event }, true, 10000, null, 204).then(() => terminalCheckpoint.clear(checkpoint)).catch(() => {});
      }
    }
    void dispose();
  });
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
  start.disabled = false;
})();
