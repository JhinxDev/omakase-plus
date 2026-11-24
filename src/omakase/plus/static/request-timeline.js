(() => {
  'use strict';
  const interval = 60_000;
  const requestSpacing = 16_000;
  const selector = '[data-request-refresh]';
  const formatter = new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
  let busy = false;
  let nextRead = 0;

  function text(container, selector, value) {
    const node = container.querySelector(selector);
    if (node && typeof value === 'string' && node.textContent !== value) node.textContent = value;
  }

  function renderTimeline(timeline, entries) {
    if (timeline && Array.isArray(entries)) {
      const steps = entries.slice(0, 8).map((step, index, all) => {
        const item = document.createElement('li');
        if (index === all.length - 1) item.setAttribute('aria-current', 'step');
        const dot = document.createElement('span');
        dot.className = 'request-timeline-dot';
        dot.setAttribute('aria-hidden', 'true');
        const label = document.createElement('span');
        label.className = 'request-timeline-label';
        label.textContent = step.label;
        item.append(dot, label);
        const date = Date.parse(step.occurred_at);
        if (Number.isFinite(date)) {
          const time = document.createElement('time');
          time.dateTime = new Date(date).toISOString();
          time.textContent = formatter.format(date);
          item.append(time);
        }
        return item;
      });
      timeline.replaceChildren(...steps);
    }
  }

  function renderTime(node, value, fallback) {
    if (!node) return;
    const date = Date.parse(value);
    if (!Number.isFinite(date)) {
      node.textContent = fallback;
      return;
    }
    const time = document.createElement('time');
    time.dateTime = new Date(date).toISOString();
    time.textContent = formatter.format(date);
    node.replaceChildren(time);
  }

  function renderDetail(container, payload) {
    const identity = container.dataset.requestIdentity;
    const entries = payload.identity ? [payload]
      : (payload.categories || []).flatMap(category => category.requests || []);
    const status = entries.find(item => item.identity === identity);
    if (!status) return;

    const node = selector => container.querySelector(selector);
    const labelNode = node('[data-request-label]');
    if (labelNode) {
      labelNode.textContent = status.display_label || status.status_label || '';
      if (status.display_tone) labelNode.dataset.statusTone = status.display_tone;
    }
    text(container, '[data-request-detail]', status.display_detail || status.detail);
    text(container, '[data-request-manual]', status.manual_note);
    const sublineWrap = node('[data-request-subline-wrap]');
    const subline = node('[data-request-subline]');
    if (sublineWrap && subline) {
      const subtext = typeof status.display_subline === 'string' ? status.display_subline : '';
      subline.textContent = subtext;
      sublineWrap.hidden = !subtext;
    }
    renderTime(node('[data-request-checked]'), status.last_checked_at, 'Not yet');
    renderTime(node('[data-request-confirmed]'), status.last_confirmed_at, 'Not yet');
    const searchPanel = node('[data-request-search]');
    if (searchPanel) {
      const transfer = node('[data-request-transfer]');
      transfer.hidden = !status.fallback_transfer?.headline;
      transfer.textContent = status.fallback_transfer?.headline || '';
      if (status.fallback_search) {
        const search = status.fallback_search;
        text(searchPanel, '[data-request-search-headline]', search.headline);
        renderTime(node('[data-request-search-checked]'), search.checked_at, '');
        node('[data-request-search-time]').hidden = !search.checked_at;
        const changed = (search.checked_at || '') !== searchPanel.dataset.requestEvidenceAt;
        const releaseList = searchPanel.querySelector('.requests-evidence');
        if (releaseList && !releaseList.contains(document.activeElement)) releaseList.hidden = changed;
        node('[data-request-evidence-reload]').hidden = !changed;
      }
    }
    for (const stage of status.stages || []) {
      const item = [...container.querySelectorAll('[data-stage]')]
        .find(item => item.dataset.stage === stage.stage);
      if (!item) continue;
      item.dataset.state = stage.state;
      if (stage.state === 'current') item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
      text(item, '.request-timeline-label', stage.label);
      const fallback = {pending: 'not yet', unknown: 'not confirmed'};
      renderTime(item.querySelector('[data-request-stage-time]'), stage.occurred_at, fallback[stage.state] || '');
    }

    const stop = node('[data-stopped-at]');
    stop.hidden = !status.stopped_at;
    stop.dataset.stoppedAt = status.stopped_at || '';
    text(stop, '[data-request-stop-label]', status.stage_label);
    const reason = stop.querySelector('[data-request-stop-reason]');
    reason.textContent = (status.unknown_reason ? status.reason_text : status.reason) || '';
    reason.classList.toggle('requests-unknown', Boolean(status.unknown_reason));
    reason.toggleAttribute('data-unknown-reason', Boolean(status.unknown_reason));
    reason.toggleAttribute('data-verified-reason', !status.unknown_reason);
    const rejections = stop.querySelector('[data-request-rejections]');
    const count = status.candidates_rejected;
    rejections.hidden = !count;
    rejections.textContent = count
      ? 'The acquisition service rejected ' + count + ' candidate' + (count === 1 ? '' : 's') + ' before it stopped.'
      : '';

    node('[data-request-check]').hidden = !status.can_check;
    node('[data-request-retry]').hidden = !status.can_retry;
    node('[data-request-no-action]').hidden = status.can_check || status.can_retry;
    const next = node('[data-request-next]');
    next.hidden = !status.next_action || status.next_action.kind === 'check';
    next.querySelector('[data-request-next-text]').textContent = status.next_action
      ? [status.next_action.label, status.next_action.reason].filter(Boolean).join(' · ')
      : '';
    const milestones = node('[data-request-milestones]');
    milestones.hidden = !status.timeline?.length;
    renderTimeline(milestones.querySelector('.request-timeline'), status.timeline);
    if (status.category === 'history') delete container.dataset.requestRefresh;
  }

  function render(container, status) {
    if (container.dataset.requestIdentity) {
      renderDetail(container, status);
      return;
    }
    text(container, '[data-request-label]', status.label);
    text(container, '[data-request-detail]', status.detail);
    const availability = container.querySelector('[data-movie-availability]');
    if (availability && status.availability) {
      text(availability, '[data-availability-label]', status.availability.label);
      text(availability, '[data-availability-detail]', status.availability.detail);
      availability.dataset.statusTone = status.availability.tone;
    }
    renderTimeline(container.querySelector('.request-timeline'), status.timeline);
    if (status.can_check === false) {
      delete container.dataset.requestRefresh;
      const check = container.querySelector('[data-request-check]');
      if (check && !check.contains(document.activeElement)) check.hidden = true;
      const actions = container.closest('.movie-detail-copy')?.querySelector('[data-request-play-slot]');
      if (actions && typeof status.play_url === 'string' && /^\/plus\/watch\/title\/movie\/tt\d+\?play=1$/.test(status.play_url)) {
        const play = document.createElement('a');
        play.className = 'button button-primary';
        play.href = status.play_url;
        play.textContent = 'Play movie';
        actions.prepend(play);
        container.querySelector('.movie-detail-status')?.setAttribute('data-status-tone', 'ready');
      }
    }
  }

  async function refresh() {
    const now = Date.now();
    if (busy || document.hidden || now < nextRead) return;
    const dueAt = item => Number(item.dataset.requestNext || 0);
    const [container] = Array.from(document.querySelectorAll(selector)).filter(item => {
      const disclosure = item.querySelector('.request-progress');
      return (item.dataset.requestIdentity || !disclosure || disclosure.open) && dueAt(item) <= now;
    }).sort((a, b) => dueAt(a) - dueAt(b));
    if (!container) return;
    busy = true;
    nextRead = now + requestSpacing;
    container.dataset.requestNext = String(now + interval);
    try {
      const response = await fetch(container.dataset.requestRefresh, {
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        signal: AbortSignal.timeout(8000),
      });
      if (response.status === 429) {
        const retryAfter = Number(response.headers.get('Retry-After'));
        const retryMs = retryAfter > 0 ? Math.min(retryAfter * 1000, interval) : requestSpacing;
        nextRead = Math.max(nextRead, Date.now() + retryMs);
        container.dataset.requestNext = String(nextRead);
      }
      if ([401, 403, 404].includes(response.status)) delete container.dataset.requestRefresh;
      if (response.ok && container.isConnected) render(container, await response.json());
    } catch {
      // Keep the last confirmed state until the next check.
    } finally {
      busy = false;
    }
  }

  for (const detail of document.querySelectorAll('[data-request-refresh][data-request-identity]')) {
    detail.dataset.requestNext = String(Date.now() + interval);
  }
  setInterval(refresh, 10_000);
  document.addEventListener('visibilitychange', refresh);
  document.addEventListener('toggle', event => {
    if (event.target.matches('.request-progress[open]')) refresh();
  }, true);
})();
