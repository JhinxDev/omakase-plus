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
            const retryable = ['event_rate_limited', 'publication_read_conflict'].includes(error.code);
            if ((error.status && !retryable) || item.keepalive || this.departing) throw error;
            if (retryable) await new Promise(resolve => setTimeout(resolve, 1000));
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
    const textTracks = player?.getTextTracks() || [];
    const selectedText = textTracks.find(track => track.active);
    const variant = player?.getVariantTracks?.().find(track => track.active);
    const audioId = selectedAudio?.id ?? variant?.audioId;
    return { audio_id: audioId != null ? String(audioId) : null, text_id: selectedText ? String(selectedText.id) : textTracks.length ? 'off' : null };
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
  function nextEpisodeUrl(value, config, origin) {
    if (typeof value !== 'string' || !Number.isSafeInteger(config.episode_number) || config.episode_number <= 0) return null;
    const tv = config.catalog_target;
    if (tv?.domain === 'tv') {
      const path = `/plus/watch/title/tv/${tv.tmdb_id}/${tv.season_number}/${config.episode_number + 1}?play=1`;
      return value === path ? new URL(path, origin).href : null;
    }
    if (!Number.isSafeInteger(config.monitor_id) || config.monitor_id <= 0 || typeof config.library_id !== 'string') return null;
    const path = `/plus/watch/${config.monitor_id}/${config.episode_number + 1}`;
    if (value.split('?')[0] !== path || value.includes('#')) return null;
    try {
      const url = new URL(value, origin);
      const keys = [...url.searchParams.keys()];
      const explicit = keys.length === 2 && url.searchParams.get('play') === '1';
      return url.origin === origin && (keys.length === 1 || explicit)
        && url.searchParams.get('library_id') === config.library_id ? url.href : null;
    } catch { return null; }
  }
  function subtitleChoices(nativeTracks, styledTracks) {
    const language = track => track.language && track.language !== 'und' ? track.language : 'Language unknown';
    const flags = track => [...(track.forced ? ['Forced'] : []), ...(track.default ? ['Default'] : [])];
    return [{ value: 'off', label: 'Off', kind: 'off' },
      ...nativeTracks.filter(track => track.id != null).map((track, index) => ({ value: `native:${track.id}`, kind: 'native',
        label: [language(track), track.label, 'Browser subtitles', ...flags(track)].filter(Boolean).join(' · ') + ` (${index + 1})` })),
      ...styledTracks.map((track, index) => ({ value: `ass:${track.plex_stream_id}`, kind: 'ass',
        label: [language(track), 'Styled ASS', ...flags(track)].join(' · ') + ` (${index + 1})` }))];
  }
  function initialSubtitleValue(nativeTracks, styledTracks, savedId, versionChanged = false) {
    const choices = subtitleChoices(nativeTracks, styledTracks);
    if (!versionChanged && savedId === 'off') return 'off';
    if (!versionChanged && savedId != null) {
      if (choices.some(choice => choice.value === savedId)) return savedId;
      if (choices.some(choice => choice.value === `native:${savedId}`)) return `native:${savedId}`;
    }
    const styled = styledTracks.find(track => track.selected);
    if (styled) return `ass:${styled.plex_stream_id}`;
    const active = nativeTracks.find(track => track.active && track.id != null);
    if (active) return `native:${active.id}`;
    return styledTracks.length ? subtitlePreference(styledTracks, null, true) : 'off';
  }
  async function switchSubtitleRenderer(value, options) {
    if (value.startsWith('ass:')) {
      const track = options.styledTracks.find(track => `ass:${track.plex_stream_id}` === value);
      if (!track) throw new Error('Subtitle track unavailable');
      options.setCustom('off'); options.selectNative(null);
      if (!await options.loadStyled(track) || !options.current()) return false;
      options.setCustom(value);
    } else {
      await options.clearStyled();
      if (!options.current()) return false;
      const track = value.startsWith('native:') ? options.nativeTracks.find(track => track.id != null && `native:${track.id}` === value) : null;
      if (value !== 'off' && !track) throw new Error('Subtitle track unavailable');
      options.setCustom(value === 'off' ? 'off' : null); options.selectNative(track);
    }
    return true;
  }
  function subtitleMenuKey(event, close) {
    // Shaka tracks pressed keys at window level. Always let releases and focus
    // navigation reach it, including keys pressed before entering this select.
    if (event.type === 'keyup') return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    else if (event.target?.tagName === 'SELECT' && !['Tab', 'Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) event.stopPropagation();
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
    const identity = config.catalog_target ? { target: config.catalog_target }
      : { monitor_id: config.monitor_id, episode_number: config.episode_number, library_id: config.library_id };
    return { ...identity, ...capabilities, defer_prepare: true };
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
    if (state.ready && !state.failed && !state.leaving) return true;
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
  function playbackTime(seconds) {
    const elapsed = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
    const hours = Math.floor(elapsed / 3600);
    const minutes = Math.floor(elapsed / 60) % 60;
    const remainder = String(elapsed % 60).padStart(2, '0');
    return hours ? hours + ':' + String(minutes).padStart(2, '0') + ':' + remainder : minutes + ':' + remainder;
  }
  function playerControlsConfiguration() {
    return {
      ...playbackRateConfiguration(), addSeekBar: false, customContextMenu: false,
      contextMenuElements: [], enableKeyboardPlaybackControls: true,
      shortcuts: { picture_in_picture: '' },
      mediaSession: { allowAutoPiP: false, supportedActions: ['play', 'pause', 'seekbackward', 'seekforward', 'seekto'] },
      documentPictureInPicture: { enabled: false },
      controlPanelElements: ['play_pause', 'omakase_next', 'omakase_time', 'spacer', 'mute', 'volume', 'fullscreen', 'overflow_menu'],
      overflowMenuButtons: ['omakase_quality', 'omakase_audio', 'omakase_subtitles', 'playback_rate'],
    };
  }
  function qualityChoices(tracks) {
    const choices = new Map();
    for (const track of tracks) {
      const key = `${track.videoId ?? ''}:${track.width ?? ''}:${track.height ?? ''}`;
      if (!choices.has(key)) choices.set(key, { key, width: track.width, height: track.height, active: false, tracks: [] });
      const item = choices.get(key); item.tracks.push(track); item.active ||= !!track.active;
    }
    return [...choices.values()].sort((a, b) => (b.height || 0) - (a.height || 0)).map(item => ({ ...item,
      label: `${choices.size === 1 ? 'Original · ' : ''}${Number.isFinite(item.width) && Number.isFinite(item.height) && item.width > 0 && item.height > 0 ? `${item.width} × ${item.height}` : 'resolution unavailable'}`,
    }));
  }
  function seekLanded(video, time) {
    return !video.seeking && Math.abs(video.currentTime - time) <= 1;
  }
  function waitForSeek(video, time) {
    if (seekLanded(video, time)) return Promise.resolve(true);
    return new Promise(resolve => {
      const events = ['seeked', 'loadeddata', 'timeupdate'];
      const finish = ok => {
        clearTimeout(timer);
        for (const event of events) video.removeEventListener(event, check);
        resolve(ok);
      };
      const check = () => { if (seekLanded(video, time)) finish(true); };
      const timer = setTimeout(() => finish(seekLanded(video, time)), 2000);
      for (const event of events) video.addEventListener(event, check);
    });
  }
  // This is a rendering clock only. Watch evidence continues through the existing
  // semantic video events and five-second sampler, never animation frames.
  class SmoothTimeline {
    constructor(video, range, clock, onRender = () => {}, options = {}) {
      this.video = video; this.range = range; this.clock = clock; this.onRender = onRender;
      this.frame = null; this.dragging = false; this.pendingValue = null; this.pointerId = null; this.listeners = [];
      this.requested = options.requested || (() => null);
      this.seekTo = typeof options.seek === 'function' ? options.seek : value => { this.video.currentTime = value; };
      this.duration = typeof options.duration === 'function' ? options.duration
        : () => Number.isFinite(this.video.duration) && this.video.duration > 0 ? this.video.duration : 0;
      this.listen = (node, name, fn) => { node.addEventListener(name, fn); this.listeners.push([node, name, fn]); };
      const update = () => { this.render(); this.schedule(); };
      for (const name of ['playing', 'pause', 'ended', 'timeupdate', 'durationchange', 'loadedmetadata', 'seeked', 'progress']) this.listen(video, name, update);
      this.listen(clock.document, 'visibilitychange', update);
      // This input is a sibling of Shaka's controls. Native range activation
      // must not reach the stage's play/pause or fullscreen click handlers.
      for (const name of ['click', 'dblclick']) this.listen(range, name, event => event.stopPropagation());
      const finishGesture = (event, commitSelection) => {
        if (!this.dragging || (this.pointerId !== null && event.pointerId !== undefined && event.pointerId !== this.pointerId)) return;
        const selected = this.pendingValue;
        this.pendingValue = null; this.pointerId = null; this.dragging = false;
        if (commitSelection && selected !== null) this.commit(selected);
        update();
      };
      this.listen(range, 'pointerdown', event => { if (this.dragging) return; this.pointerId = event.pointerId ?? null; this.pendingValue = null; this.dragging = true; this.schedule(); });
      this.listen(range, 'input', () => { this.pendingValue = Number(range.value); this.dragging = true; this.render(); });
      this.listen(range, 'change', () => { const selected = this.pendingValue ?? Number(range.value); this.pendingValue = null; this.pointerId = null; this.dragging = false; this.commit(selected); update(); });
      this.listen(range, 'pointerup', event => finishGesture(event, true));
      this.listen(clock.document, 'pointerup', event => finishGesture(event, true));
      this.listen(range, 'lostpointercapture', event => finishGesture(event, true));
      this.listen(range, 'pointercancel', event => finishGesture(event, false));
      this.listen(clock.document, 'pointercancel', event => finishGesture(event, false));
      this.listen(range, 'blur', event => finishGesture(event, false));
      this.listen(range, 'keydown', event => {
        if (event.altKey || event.ctrlKey || event.metaKey) return;
        if (event.key === ' ') { event.stopPropagation(); return; }
        const offsets = { ArrowLeft: -5, ArrowDown: -5, ArrowRight: 5, ArrowUp: 5, PageDown: -30, PageUp: 30 };
        if (!(event.key in offsets) && !['Home', 'End'].includes(event.key)) return;
        event.preventDefault(); event.stopPropagation();
        this.commit(event.key === 'Home' ? 0 : event.key === 'End' ? this.duration() : (this.requested() ?? video.currentTime) + offsets[event.key]);
        this.dragging = false; this.pendingValue = null; this.pointerId = null; update();
      });
      // Keyup still reaches the supplied controls so their pressed-key state clears.
      update();
    }
    commit(value) {
      const duration = this.duration();
      if (!this.range.disabled && Number.isFinite(value) && duration > 0) {
        const target = Math.max(0, Math.min(duration, value));
        if (Math.abs(this.video.currentTime - target) > .000001) this.seekTo(target);
      }
    }
    render() {
      const duration = this.duration();
      const requested = this.requested();
      const actual = Math.max(0, Math.min(duration, Number(this.video.currentTime) || 0));
      const value = Math.max(0, Math.min(duration, Number(this.dragging ? this.range.value : (requested ?? actual)) || 0));
      this.range.max = String(duration || 1); if (!this.dragging) this.range.value = String(value);
      this.range.style.setProperty('--played', `${duration ? actual / duration * 100 : 0}%`);
      let buffered = actual;
      for (let i = 0; i < this.video.buffered.length; i++) if (this.video.buffered.start(i) <= actual + .1 && this.video.buffered.end(i) >= actual) buffered = this.video.buffered.end(i);
      this.range.style.setProperty('--buffered', `${duration ? Math.min(100, buffered / duration * 100) : 0}%`);
      this.range.setAttribute('aria-valuetext', playbackTime(value) + (requested == null ? ' of ' : ' requested, duration ') + playbackTime(duration));
      this.onRender(value, duration); // Also drives validated segment visibility, without writes.
    }
    schedule() {
      if (this.frame !== null) { this.clock.cancel(this.frame); this.frame = null; }
      if (this.dragging || this.video.paused || this.video.ended || this.clock.document.hidden || this.clock.reducedMotion()) return;
      this.frame = this.clock.request(() => { this.frame = null; this.render(); this.schedule(); });
    }
    destroy() {
      if (this.frame !== null) this.clock.cancel(this.frame); this.frame = null;
      for (const [node, name, fn] of this.listeners) node.removeEventListener(name, fn);
      this.listeners = [];
    }
  }
  function validatedSkipSegments(session) {
    const data = session?.skip_segments, source = session?.source;
    if (!data || !source || data.media_version !== source.media_version || data.episode_number !== source.episode_number || !Array.isArray(data.segments) || data.segments.length > 4) return [];
    const duration = source.duration_ms / 1000;
    if (!Number.isFinite(duration) || duration <= 0) return [];
    let end = 0;
    const items = [];
    for (const item of data.segments) {
      if (!item || typeof item !== 'object' || Array.isArray(item) || !['intro', 'credits'].includes(item.kind) || !Number.isFinite(item.start_seconds) || !Number.isFinite(item.end_seconds) || item.start_seconds < end || item.end_seconds <= item.start_seconds || item.end_seconds > duration) return [];
      end = item.end_seconds; items.push({ kind: item.kind, start_seconds: item.start_seconds, end_seconds: end });
    }
    return items;
  }
  function activeSkipSegment(segments, time) { return segments.find(item => time >= item.start_seconds && time < item.end_seconds) || null; }
  // Public SettingsMenu lifecycle owns sibling visibility and Back focus. Keep
  // these production types separately constructible for lifecycle regressions.
  function settingsMenuTypes(shaka, hooks) {
    const { document, find } = hooks;
    class QualityMenu extends shaka.ui.SettingsMenu {
      constructor(parent, controls) {
        super(parent, controls, '');
        this.button.querySelector('.shaka-ui-icon')?.remove();
        const badge = document.createElement('span'); badge.className = 'omakase-caption-icon'; badge.textContent = 'Q'; badge.setAttribute('aria-hidden', 'true'); this.button.prepend(badge);
        this.nameSpan.textContent = 'Quality'; this.backSpan.textContent = 'Quality';
        this.button.setAttribute('aria-label', 'Quality'); this.menu.classList.add('omakase-quality-menu');
        this.refresh = () => this.checkAvailability();
        for (const event of ['trackschanged', 'variantchanged', 'adaptation']) this.player.addEventListener(event, this.refresh);
        hooks.register(this); this.checkAvailability();
      }
      checkAvailability() {
        super.checkAvailability();
        const { ready, failed } = hooks.state();
        const choices = qualityChoices(this.player.getVariantTracks());
        this.button.disabled = !ready || failed || !choices.length;
        this.currentSelection.textContent = choices.find(item => item.active)?.label || 'Original';
        for (const node of this.menu.querySelectorAll('[data-quality-choice]')) node.remove();
        for (const choice of choices) {
          const button = document.createElement('button'); button.type = 'button'; button.dataset.qualityChoice = choice.key;
          button.textContent = choice.label; button.setAttribute('aria-pressed', String(choice.active));
          button.disabled = choices.length === 1;
          button.addEventListener('click', () => {
            const active = this.player.getVariantTracks().find(track => track.active);
            const selected = choice.tracks.find(track => track.audioId === active?.audioId) || choice.tracks[0];
            this.player.configure({ abr: { enabled: false } }); this.player.selectVariantTrack(selected, true);
          });
          this.menu.appendChild(button);
        }
      }
      release() { for (const event of ['trackschanged', 'variantchanged', 'adaptation']) this.player.removeEventListener(event, this.refresh); hooks.unregister(this); super.release(); }
    }
    class AudioMenu extends shaka.ui.AudioLanguageSelection {
      constructor(parent, controls) {
        super(parent, controls); this.button.classList.add('omakase-audio-button'); this.nameSpan.textContent = 'Audio';
        this.refresh = () => this.checkAvailability(); this.player.addEventListener('audiotrackschanged', this.refresh);
        hooks.register(this); this.checkAvailability();
      }
      updateLocalizedStrings() { super.updateLocalizedStrings(); this.button.setAttribute('aria-label', 'Audio language'); this.backSpan.textContent = 'Audio language'; }
      checkAvailability() {
        super.checkAvailability();
        const { ready, failed } = hooks.state();
        this.button.disabled = !ready || failed || !this.player.getAudioTracks().length;
        this.button.title = this.player.getAudioTracks().length ? 'Choose audio language' : 'Audio is unavailable for this stream';
      }
      release() { this.player.removeEventListener('audiotrackschanged', this.refresh); hooks.unregister(this); super.release(); }
    }
    class SubtitlesMenu extends shaka.ui.SettingsMenu {
      constructor(parent, controls) {
        super(parent, controls, ''); this.button.classList.add('omakase-subtitles-button');
        this.button.querySelector('.shaka-ui-icon')?.remove(); const badge = document.createElement('span'); badge.className = 'omakase-caption-icon'; badge.textContent = 'CC'; badge.setAttribute('aria-hidden', 'true'); this.button.prepend(badge);
        this.button.setAttribute('aria-label', 'Subtitles'); this.button.setAttribute('aria-haspopup', 'menu');
        this.nameSpan.textContent = 'Subtitles'; this.backSpan.textContent = 'Subtitles'; this.backButton.setAttribute('aria-label', 'Close subtitles');
        this.menu.classList.add('omakase-subtitles-menu'); this.menu.setAttribute('role', 'menu'); this.menu.setAttribute('aria-label', 'Subtitles'); this.menu.id = 'web-player-subtitles-menu'; this.button.setAttribute('aria-controls', this.menu.id);
        this.choiceRows = [];
        this.choiceKey = null;
        this.menu.addEventListener('keydown', event => {
          if (event.key === 'Escape') {
            event.preventDefault(); event.stopPropagation(); this.backButton.click();
          }
        });
        hooks.register(this); this.checkAvailability();
      }
      checkAvailability() {
        super.checkAvailability();
        const { ready, failed, captionLoading } = hooks.state();
        const selector = find('ass-select');
        const choices = Array.from(selector.options, option => ({ value: option.value, label: option.textContent }));
        const key = JSON.stringify(choices);
        this.button.disabled = !ready || failed;
        this.menu.setAttribute('aria-busy', String(captionLoading));
        this.currentSelection.textContent = choices.length === 1 ? 'No tracks · Off' : choices.find(choice => choice.value === selector.value)?.label || 'Off';
        if (key !== this.choiceKey) {
          for (const row of this.choiceRows) row.remove();
          this.choiceRows = choices.map(choice => {
            const row = document.createElement('button'); row.type = 'button'; row.dataset.subtitleChoice = choice.value;
            row.setAttribute('role', 'menuitemradio'); row.textContent = choice.label;
            row.addEventListener('click', event => {
              event.stopPropagation();
              if (!row.disabled) void hooks.selectSubtitle(choice.value).then(() => {
                if (this.isSubMenuOpened && !this.menu.classList.contains('shaka-hidden') &&
                    (document.activeElement === row || document.activeElement === document.body || !document.activeElement?.isConnected)) row.focus();
              }).catch(() => {});
            });
            this.menu.appendChild(row); return row;
          });
          this.choiceKey = key;
        }
        for (const row of this.choiceRows) {
          const selected = row.dataset.subtitleChoice === selector.value;
          row.disabled = this.button.disabled || captionLoading; row.setAttribute('aria-checked', String(selected));
          row.classList.toggle('shaka-chosen-item', selected);
        }
      }
      release() { hooks.unregister(this); super.release(); }
    }
    return { QualityMenu, AudioMenu, SubtitlesMenu };
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { playbackTime, settingsMenuTypes, playerControlsConfiguration, qualityChoices, SmoothTimeline, validatedSkipSegments, activeSkipSegment, EventQueue, selectedTracks, browserCapabilities, subtitlePreference, audioPreference, eventPosition, playbackRateConfiguration, styledSubtitleTracks, enhanceRangeControls, SessionPreparation, startPayload, exitSnapshot, TerminalCheckpoint, enforcePlayback, guardedPlay, nextEpisodeUrl, subtitleChoices, initialSubtitleValue, subtitleMenuKey, switchSubtitleRenderer };
  if (typeof document === 'undefined') return;
  const root = document.querySelector('[data-web-player]');
  if (!root) return;
  const movedNodes = new Map();
  const find = name => root.querySelector(`[data-player-${name}]`) || movedNodes.get(name) || null;
  for (const name of ['ass-options', 'ass-select', 'ass-status', 'caption-mode-note', 'subtitle-unavailable', 'next']) movedNodes.set(name, find(name));
  const config = JSON.parse(document.getElementById('web-player-config').textContent);
  const video = find('video');
  const container = find('container');
  const status = find('status');
  const saved = find('save');
  const start = find('start');
  const startPanel = find('start-panel');
  const loading = find('loading');
  const restart = find('restart');
  const retry = find('retry');
  const reload = find('reload');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  let session = null;
  let player = null;
  let mediaUri = null;
  let timeline = null;
  let seekTo = null;
  let activeSeekSession = null;
  let seekOrigin = null;
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
  let controlsRegistered = false;
  const barItems = new Set();
  let nextCandidate = config.next_url;
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
        error.code = body?.code || (body?.detail === 'publication_read_conflict' ? body.detail : null);
        error.source = body?.source;
        throw error;
      }
      if (expectedStatus !== null && response.status !== expectedStatus) throw new Error('Playback save acknowledgement unavailable');
      return response.status === 204 ? {} : await response.json();
    } finally { window.clearTimeout(timeout); externalSignal?.removeEventListener('abort', abort); }
  }
  function sessionPath(suffix = '') { return `/plus/api/playback/sessions/${encodeURIComponent(session.session_id)}${suffix}`; }
  function setLoading(label = '', opening = false) {
    loading.hidden = !label;
    loading.classList.toggle('is-opening', opening);
    if (label) find('loading-label').textContent = label;
  }
  function showError(error, saving = false) {
    setLoading();
    startPanel.hidden = true;
    failed = true;
    // Stop event capture before pause, so a failing save cannot enqueue another write.
    ready = false;
    find('timeline').disabled = true; find('skip').hidden = true;
    find('ass-select').disabled = true;
    refreshBarControls();
    video.pause();
    void stopCaptions().catch(() => {});
    window.clearInterval(periodic);
    const blocked = [401, 403, 409].includes(error.status);
    find('error').hidden = false;
    const descriptions = {
      unsupported_browser: 'This browser cannot decode the selected video track.',
      unsupported_browser_audio: 'This browser cannot decode the selected audio track.',
      unsupported_codec: 'This browser cannot decode this video or audio format.',
      unsupported_source: 'This source format is unavailable in the web player.',
      styled_subtitles_unsupported: 'This browser cannot display the styled subtitles on this version.',
      stream_start_timeout: 'The media server took too long to open this title.',
      stream_start_failed: 'The media server could not start the stream.',
      saved_position_unavailable: 'The stream did not reach your saved position. Try again in a moment.',
      stream_worker_unavailable: 'The streaming worker is temporarily unavailable.',
      stream_cache_unavailable: 'The server could not open its streaming cache.',
      stream_capacity: 'This copy is too large to prepare here. You can open it in Plex from Playback options.',
      publication_read_conflict: 'The library was updating while this title opened.',
    };
    find('error-text').textContent = blocked
      ? 'Your session or library access changed. Reload the player to check access again.'
      : saving ? 'Progress could not be saved. Reload to continue with your last confirmed save.'
        : descriptions[error.code] || (error.status === 415 ? 'This version is unavailable in the web player.'
          : error.status === 503 ? 'The server could not prepare this title.'
          : 'The player could not load this stream. Check your connection and try again.');
    if (safeLink(find('plex'), error.source?.plex_url)) find('fallback').hidden = true;
    status.textContent = 'Playback paused.';
    reload.hidden = !(blocked || saving);
    retry.hidden = blocked || saving || error.status === 415 || triedAgain;
    restart.disabled = true;
    start.disabled = true;
    if (saving) saved.textContent = 'Progress save interrupted.';
  }
  function tracks() {
    // These are Shaka track IDs for this media version, never Plex stream IDs.
    return { ...selectedTracks(player), ...(customTextId != null ? { text_id: customTextId } : {}) };
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
    if (!ready || !queue || activeSeekSession === session) return;
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
    const styledActive = typeof customTextId === 'string' && customTextId.startsWith('ass:');
    video.disablePictureInPicture = true;
    overlay.configure(playerControlsConfiguration());
    enhanceRangeControls(container);
    find('caption-mode-note').hidden = !styledActive;
    refreshBarControls();
  }
  function refreshBarControls() {
    const next = nextEpisodeUrl(nextCandidate, config, window.location.origin);
    const sidebar = find('sidebar-next');
    sidebar.hidden = !next;
    if (next) sidebar.href = next;
    else sidebar.removeAttribute('href');
    for (const item of barItems) { item.checkAvailability(); if ((!ready || failed) && item.menu) { item.menu.classList.add('shaka-hidden'); item.button.setAttribute('aria-expanded', 'false'); } }
  }
  function registerBarControls() {
    if (controlsRegistered) return;
    controlsRegistered = true;
    const { QualityMenu, AudioMenu, SubtitlesMenu } = settingsMenuTypes(shaka, {
      document, find, state: () => ({ ready, failed, captionLoading }),
      register: item => barItems.add(item), unregister: item => barItems.delete(item),
      selectSubtitle: id => changeCaption(id),
    });
    class NextEpisode extends shaka.ui.Element {
      constructor(parent, controls) { super(parent, controls); this.link = find('next'); parent.appendChild(this.link); this.link.hidden = config.episode_number === 0; barItems.add(this); this.checkAvailability(); }
      checkAvailability() {
        const candidate = nextEpisodeUrl(nextCandidate, config, window.location.origin);
        const enabled = !!candidate && ready && !failed;
        this.link.removeAttribute('href'); if (enabled) this.link.href = candidate;
        this.link.setAttribute('aria-disabled', String(!enabled)); this.link.setAttribute('aria-label', candidate ? 'Next episode' : 'Next episode unavailable');
        this.link.querySelector('[data-next-label]').textContent = candidate ? 'Next episode' : 'No next episode';
        this.link.querySelector('[data-next-compact]').textContent = candidate ? 'Next' : 'No next';
        this.link.title = candidate ? 'Open the next episode in this series and library' : 'No next episode is available in this library';
      }
      release() { barItems.delete(this); super.release(); }
    }
    for (const [name, Type] of [['omakase_quality', QualityMenu], ['omakase_audio', AudioMenu], ['omakase_subtitles', SubtitlesMenu]]) shaka.ui.OverflowMenu.registerElement(name, { create: (parent, controls) => new Type(parent, controls) });
    shaka.ui.Controls.registerElement('omakase_next', { create: (parent, controls) => new NextEpisode(parent, controls) });
    shaka.ui.Controls.registerElement('omakase_time', { create: parent => {
      const clock = find('clock');
      parent.appendChild(clock);
      return { release() { find('bar-storage').appendChild(clock); } };
    } });
  }
  async function stopCaptions() {
    captionIntent++;
    const old = captions;
    captions = null;
    captionLoading = false;
    captionTasks = captionTasks.catch(() => {}).then(async () => { if (old) await old.destroy(); });
    await captionTasks;
  }
  function captionUnavailable() {
    customTextId = 'off';
    void captions?.clear().catch(() => {});
    find('ass-select').value = 'off';
    find('ass-status').textContent = 'Subtitles unavailable.';
    refreshBarControls();
  }

  function changeCaption(id) {
    const intent = ++captionIntent;
    const source = session;
    captionLoading = true;
    find('ass-status').textContent = id === 'off' ? 'Turning subtitles off…' : id.startsWith('native:') ? 'Preparing browser subtitles…' : 'Preparing styled subtitles…';
    find('ass-select').disabled = true;
    if (id.startsWith('ass:')) video.disablePictureInPicture = true;
    refreshBarControls();
    const task = captionTasks.catch(() => {}).then(async () => {
      if (intent !== captionIntent || source !== session || leaving) return;
      try {
        const applied = await switchSubtitleRenderer(id, {
          nativeTracks: player.getTextTracks(), styledTracks: assTracks,
          clearStyled: async () => { if (captions) await captions.clear(); },
          selectNative: track => player.selectTextTrack(track), setCustom: value => { customTextId = value; },
          current: () => intent === captionIntent && source === session && !leaving,
          loadStyled: async track => {
          if (!browserCapabilities(window, video).ass_supported) throw Object.assign(new Error('Styled subtitles unavailable'), { code: 'ass_display' });
          if (!captions) {
            const { OmakaseASS } = await import('/plus/static/web-ass.js');
            if (intent !== captionIntent || source !== session || leaving) return;
            const controller = new OmakaseASS({ video, container, onError: () => {
              if (captions === controller && source === session && !leaving) captionUnavailable();
            } });
            captions = controller;
          }
          const success = await captions.setTrack({ url: track.url, fonts: source.fonts || [], timeOffset: 0 });
          return success;
          },
        });
        if (!applied) return;
        if (intent !== captionIntent || source !== session || leaving) return;
        if (id.startsWith('ass:')) customTextId = id;
        find('ass-select').value = id;
        find('ass-status').textContent = id === 'off' ? (assTracks.length ? 'Styled subtitles are off.' : 'Subtitles are off.') : id.startsWith('native:') ? 'Browser subtitle track selected.' : 'Styled subtitles ready.';
        captionLoading = false;
        refreshBarControls();
        capture('progress');
      } catch {
        if (intent === captionIntent && source === session && !leaving) captionUnavailable();
      } finally { if (intent === captionIntent) { captionLoading = false; find('ass-select').disabled = !ready || failed; refreshBarControls(); } }
    });
    captionTasks = task;
    return task;
  }
  async function watchStreamSubtitles(target) {
    if (!target?.streaming || !target.subtitle_url || !target.subtitle_track) return;
    const current = () => !leaving && !failed && session === target;
    find('subtitle-unavailable').hidden = true;
    find('ass-status').textContent = 'Subtitles are still preparing.';
    const deadline = performance.now() + 130000;
    let delay = 750;
    while (current() && performance.now() < deadline) {
      try {
        const response = await fetch(target.subtitle_url, {
          method: 'HEAD', credentials: 'same-origin', cache: 'no-store',
          signal: AbortSignal.timeout(5000),
        });
        if (!current()) return;
        if (response.status === 200) {
          target.subtitles = [{ ...target.subtitle_track, url: target.subtitle_url }];
          await prepareTracks();
          return;
        }
        if (![202, 429, 503].includes(response.status)) break;
      } catch { /* A timed-out status check can be retried. */ }
      await new Promise(resolve => window.setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 5000);
    }
    if (current()) captionUnavailable();
  }
  async function prepareTracks() {
    assTracks = styledSubtitleTracks(session.subtitles || []);
    customTextId = null;
    const selector = find('ass-select');
    selector.disabled = true;
    const nativeTracks = player.getTextTracks();
    selector.replaceChildren(new Option('Off', 'off'));
    for (const [kind, label] of [['native', 'Browser subtitles'], ['ass', 'Styled ASS']]) {
      const options = subtitleChoices(nativeTracks, assTracks).filter(choice => choice.kind === kind);
      if (options.length) { const group = document.createElement('optgroup'); group.label = label; for (const option of options) group.appendChild(new Option(option.label, option.value)); selector.appendChild(group); }
    }
    find('subtitle-unavailable').hidden = !!(nativeTracks.length || assTracks.length);
    const audio = audioPreference(player, session);
    if (audio?.native) player.selectAudioTrack(audio.native);
    else if (audio?.variant) player.selectVariantTrack(audio.variant, true);
    const initial = initialSubtitleValue(nativeTracks, assTracks, session.text_id, session.resume_version_changed);
    refreshBarControls();
    await changeCaption(initial);
  }
  async function dispose() {
    ready = false;
    if (timeline) timeline.destroy(); timeline = null;
    seekTo = null;
    find('timeline').disabled = true; find('skip').hidden = true;
    refreshBarControls();
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
    video.disablePictureInPicture = true;
    if (overlay) await overlay.destroy();
    if (player) await player.destroy();
    overlay = null;
    player = null;
  }
  async function playReady() {
    if (!ready || leaving || failed) return;
    try {
      await video.play();
    } catch (error) {
      if (!ready || leaving || failed) return;
      if (error.name !== 'NotAllowedError') {
        showError(error);
        return;
      }
      setLoading();
      find('start-heading').textContent = 'Press Play to start';
      find('resume-option').hidden = true;
      start.disabled = false;
      startPanel.hidden = false;
      status.textContent = 'Press Play to continue.';
    }
  }
  async function begin() {
    if (starting || leaving) return;
    starting = true;
    const currentAttempt = ++attempt;
    failed = false;
    start.disabled = true;
    startPanel.hidden = true;
    find('cancel-preparation').hidden = true;
    const mediaLabel = config.episode_number ? 'episode' : 'movie';
    setLoading(`Loading ${mediaLabel}…`, true);
    retry.hidden = true;
    find('error').hidden = true;
    status.textContent = `Preparing your ${mediaLabel}…`;
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
      status.textContent = `Opening your ${mediaLabel}…`;
      const startUrl = config.catalog_target ? '/plus/api/playback/catalog-sessions' : '/plus/api/playback/sessions';
      const opened = await post(startUrl, startPayload(config, browserCapabilities(window, video)), false, 40000);
      if (!opened || typeof opened.session_id !== 'string' || !opened.session_id) throw new Error('Invalid playback session');
      if (currentAttempt !== attempt || leaving) {
        void post(`/plus/api/playback/sessions/${encodeURIComponent(opened.session_id)}/close`, {}, true).catch(() => {});
        return;
      }
      session = opened;
      if (!Number.isFinite(session.duration_seconds) || session.duration_seconds <= 0) throw new Error('Invalid playback timeline');
      if (safeLink(find('plex'), session.source?.plex_url)) find('fallback').hidden = true;
      if (session.preparation_required) {
        const pending = new SessionPreparation(post, session, () => {
          setLoading(`Preparing ${mediaLabel}…`, true);
          status.textContent = `Preparing this ${mediaLabel}. This can take up to 45 seconds.`;
          find('cancel-preparation').hidden = false;
          find('cancel-preparation').disabled = false;
        });
        preparation = pending;
        const prepared = await pending.resolve();
        if (currentAttempt !== attempt || leaving) return;
        preparation = null;
        session = prepared;
        find('cancel-preparation').hidden = true;
        if (safeLink(find('plex'), session.source?.plex_url)) find('fallback').hidden = true;
      }
      if (currentAttempt !== attempt || leaving) return;
      nextCandidate = session.next_url;
      const media = new URL(session.media_url, window.location.origin);
      if (media.origin !== window.location.origin || !['http:', 'https:'].includes(media.protocol)) throw new Error('Invalid media source');
      mediaUri = media.href;
      player = new shaka.Player();
      await player.attach(video);
      registerBarControls();
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
      if (session.streaming && position > 0 && !seekLanded(video, position)) {
        setLoading('Finding your saved position…', true);
        status.textContent = 'Preparing your saved position…';
        const deadline = Date.now() + 45000;
        const unavailable = Object.assign(new Error('Saved position unavailable'), { code: 'saved_position_unavailable' });
        for (;;) {
          if (currentAttempt !== attempt || failed || leaving) return;
          const range = player.seekRange();
          if (position >= range.start - 1 && position <= range.end + 1) break;
          if (Date.now() >= deadline) throw unavailable;
          await new Promise(resolve => window.setTimeout(resolve, 250));
        }
        if (!seekLanded(video, position)) {
          await player.load(media.href, position, session.media_type || undefined);
          if (currentAttempt !== attempt || failed || leaving) return;
          if (!(await waitForSeek(video, position))) throw unavailable;
        }
      }
      if (currentAttempt !== attempt || failed || leaving) return;
      await prepareTracks();
      if (failed || leaving) return;
      queue = new EventQueue(
        (payload, keepalive) => post(sessionPath('/events'), payload, keepalive),
        error => showError(error, true),
        (_result, state) => { if (!failed && state.latest) saved.textContent = priorExitUnconfirmed ? 'Current position saved. Previous exit unverified.' : 'Progress saved.'; },
        275,
      );
      ready = true;
      if (session.streaming) void watchStreamSubtitles(session);
      find('timeline').disabled = false;
      const currentSession = session;
      let pendingSeek = null;
      let seekTimer = null;
      let seeking = false;
      const seekCurrent = () => session === currentSession && ready && !failed && !leaving;
      function scheduleSeek(delay) {
        window.clearTimeout(seekTimer);
        seekTimer = window.setTimeout(() => void applySeek(), delay);
      }
      async function applySeek() {
        if (!seekCurrent() || seeking || !pendingSeek) return;
        seeking = true;
        let resume = false;
        let settled = false;
        try {
          await queue.idle();
          while (seekCurrent() && pendingSeek) {
            const request = pendingSeek;
            const seekEnd = player.seekRange().end;
            if (currentSession.streaming && Number.isFinite(seekEnd) && request.time > seekEnd + 1) {
              if (Date.now() < request.deadline) {
                status.textContent = 'Preparing ' + playbackTime(request.time) + '…';
                return;
              }
              pendingSeek = null;
              status.textContent = 'That part of the video is still preparing. Try seeking again.';
              return;
            }
            if (activeSeekSession !== currentSession) {
              resume = !video.paused;
              seekOrigin = playbackSnapshot('pause');
              capture('pause', false, false);
              activeSeekSession = currentSession;
              video.pause();
            }
            const buffered = Array.from({length: video.buffered.length}, (_, i) => i).some(
              i => video.buffered.start(i) <= request.time && request.time < video.buffered.end(i)
            );
            if (!currentSession.streaming || buffered) video.currentTime = request.time;
            else await player.load(mediaUri, request.time, currentSession.media_type || undefined);
            if (!seekCurrent()) return;
            settled = await waitForSeek(video, request.time);
            if (!seekCurrent()) return;
            if (pendingSeek !== request) continue;
            pendingSeek = null;
            if (!settled) {
              status.textContent = 'Could not reach that position. Try seeking again.';
              return;
            }
            status.textContent = resume ? 'Playing' : 'Paused';
          }
        } catch (error) {
          if (seekCurrent()) showError(error);
        } finally {
          seeking = false;
          if (activeSeekSession === currentSession) {
            activeSeekSession = null;
            seekOrigin = null;
            if (seekCurrent()) {
              if (settled) capture('seek', false, false);
              if (resume) video.play().catch(() => {});
            }
          }
          if (seekCurrent()) {
            timeline.render();
            if (pendingSeek) scheduleSeek(250);
            else setLoading();
          }
        }
      }
      seekTo = time => {
        if (!seekCurrent()) return;
        pendingSeek = {time, deadline: Date.now() + 45000};
        status.textContent = 'Seeking to ' + playbackTime(time) + '…';
        setLoading(status.textContent);
        timeline.render();
        if (!seeking) scheduleSeek(180);
      };
      timeline = new SmoothTimeline(video, find('timeline'), {
        request: callback => window.requestAnimationFrame(callback), cancel: id => window.cancelAnimationFrame(id),
        document, reducedMotion: () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
      }, (_position, duration) => {
        const clock = find('clock');
        const label = playbackTime(video.currentTime) + ' / ' + playbackTime(duration);
        if (clock.textContent !== label) clock.textContent = label;
        const marker = ready && !failed && !pendingSeek && !video.seeking ? activeSkipSegment(validatedSkipSegments(session), video.currentTime) : null;
        find('skip').hidden = !marker;
        if (marker) find('skip').textContent = marker.kind === 'intro' ? 'Skip Intro' : 'Skip Credits';
      }, {
        // An event playlist grows as segments publish. The exact session duration
        // defines the control scale while the video element reports buffer state.
        duration: () => session.duration_seconds,
        requested: () => pendingSeek?.time,
        seek: seekTo,
      });
      find('ass-select').disabled = false;
      refreshBarControls();
      player.addEventListener('variantchanged', () => capture('progress'));
      player.addEventListener('textchanged', () => {
        if (!captionLoading && customTextId == null) { const active = player.getTextTracks().find(track => track.active); find('ass-select').value = active ? 'native:' + active.id : 'off'; }
        capture('progress');
      });
      restart.disabled = false;
      find('start-panel').hidden = true;
      status.textContent = session.source?.mode === 'direct' ? 'Ready to play · Direct stream' : 'Ready to play';
      capture('seek');
      periodic = window.setInterval(() => { if (!video.paused && !video.ended) capture('progress'); }, 5000);
      await playReady();
    } catch (error) { if (currentAttempt === attempt && !leaving) showError(error); }
    finally {
      if (currentAttempt === attempt) {
        starting = false;
        find('cancel-preparation').hidden = true;
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
      find('resume-option').hidden = false;
      find('cancel-preparation').hidden = true;
      setLoading();
      startPanel.hidden = false;
      container.removeAttribute('aria-busy');
      find('start-heading').textContent = 'Preparation cancelled';
      status.textContent = 'Preparation cancelled. Playback has not started.';
      start.focus();
    } catch (error) {
      if (cancellation === attempt && !leaving) {
        starting = false;
        find('cancel-preparation').hidden = true;
        container.removeAttribute('aria-busy');
        showError(error);
        reload.hidden = false;
      }
    }
  });
  start.addEventListener('click', () => { if (ready) void playReady(); else void begin(); });
  retry.addEventListener('click', () => { if (!triedAgain) { triedAgain = true; void begin(); } });
  reload.addEventListener('click', () => window.location.reload());
  find('ass-select').addEventListener('change', event => {
    if (!ready || failed || leaving) return;
    const fromBar = !!event.target.closest('.omakase-subtitles-menu');
    void changeCaption(event.target.value).then(() => {
      if (fromBar && (document.activeElement === document.body || document.activeElement === event.target || !document.activeElement?.isConnected)) container.querySelector('.omakase-subtitles-button')?.focus();
    }).catch(() => {});
  });
  find('next').addEventListener('click', event => { if (find('next').getAttribute('aria-disabled') === 'true' || !find('next').hasAttribute('href')) event.preventDefault(); event.stopPropagation(); });
  find('next').addEventListener('keydown', event => { if (['Enter', ' '].includes(event.key)) event.stopPropagation(); });
  restart.addEventListener('click', () => { seekTo?.(0); });
  find('skip').addEventListener('click', event => {
    event.stopPropagation();
    const marker = ready && !failed && !video.seeking ? activeSkipSegment(validatedSkipSegments(session), video.currentTime) : null;
    if (marker) { seekTo?.(marker.end_seconds); find('skip').hidden = true; }
  });
  video.addEventListener('playing', () => {
    if (!enforcePlayback(video, playbackState())) return;
    const moveFocus = document.activeElement === start;
    startPanel.hidden = true;
    if (moveFocus) container.querySelector('.shaka-play-button')?.focus();
    if (activeSeekSession !== session) { setLoading(); status.textContent = 'Playing'; capture('play'); }
  });
  video.addEventListener('play', () => { enforcePlayback(video, playbackState()); });
  function unsupportedCaptionMode() {
    if (typeof customTextId === 'string' && customTextId.startsWith('ass:')) captionUnavailable();
  }
  video.addEventListener('webkitbeginfullscreen', unsupportedCaptionMode);
  video.addEventListener('enterpictureinpicture', () => {
    if (document.pictureInPictureElement) void document.exitPictureInPicture().catch(() => {});
  });
  video.addEventListener('pause', () => { if (ready && activeSeekSession !== session && !leaving && !video.ended) { setLoading(); status.textContent = 'Paused'; capture('pause'); } });
  video.addEventListener('waiting', () => { if (ready && activeSeekSession !== session && loading.hidden) { setLoading('Buffering…'); status.textContent = 'Buffering…'; capture('pause'); } });
  video.addEventListener('seeking', () => capture('seek'));
  video.addEventListener('seeked', () => capture(video.paused ? 'seek' : 'play'));
  video.addEventListener('ratechange', () => {
    capture('progress', false, false);
    // Preset buttons and shortcuts also update the slider without native input.
    container.querySelectorAll('.shaka-playback-rate-slider').forEach(range => range.setAttribute('aria-valuetext', `${video.playbackRate} times normal speed`));
  });
  video.addEventListener('ended', () => { if (ready) { status.textContent = 'Playback finished. Saving your position…'; capture('ended'); } });
  // Navigation to another episode still requires a viewer action.
  window.addEventListener('pagehide', () => {
    if (leaving) return;
    leaving = true;
    if (ready && queue && session) {
      const snapshot = activeSeekSession === session ? seekOrigin : playbackSnapshot('pause');
      if (snapshot) {
        // A single immediate request owns the paced terminal save on the server;
        // no document timer or queued callback is required after navigation.
        const final_event = queue.seal(exitSnapshot(snapshot, queue, video.seeking || activeSeekSession === session, video.ended));
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
  if (config.start_immediately === true) {
    const startWhenVisible = () => {
      if (document.hidden || document.prerendering) return;
      document.removeEventListener('visibilitychange', startWhenVisible);
      document.removeEventListener('prerenderingchange', startWhenVisible);
      void begin();
    };
    document.addEventListener('visibilitychange', startWhenVisible);
    document.addEventListener('prerenderingchange', startWhenVisible);
    startWhenVisible();
  }
})();
