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

  function render(container, status) {
    text(container, '[data-request-label]', status.label);
    text(container, '[data-request-detail]', status.detail);
    const timeline = container.querySelector('.request-timeline');
    if (timeline && Array.isArray(status.timeline)) {
      const steps = status.timeline.slice(0, 8).map((step, index, all) => {
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
      return (!disclosure || disclosure.open) && dueAt(item) <= now;
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
      if (response.status === 404) delete container.dataset.requestRefresh;
      if (response.ok && container.isConnected) render(container, await response.json());
    } catch {
      // Keep the last confirmed state until the next check.
    } finally {
      busy = false;
    }
  }

  setInterval(refresh, 10_000);
  document.addEventListener('visibilitychange', refresh);
  document.addEventListener('toggle', event => {
    if (event.target.matches('.request-progress[open]')) refresh();
  }, true);
})();
