(() => {
  'use strict';
  const screen = document.querySelector('[data-requests-screen]');
  if (!screen) return;
  const interval = 60_000;
  const spacing = 16_000;
  const formatter = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  let busy = false;
  let nextRead = Date.now() + interval;
  function setText(node, value) {
    if (node && typeof value === 'string' && node.textContent !== value) node.textContent = value;
  }
  function render(payload) {
    let changed = false;
    for (const category of payload.categories || []) {
      const section = screen.querySelector('[data-request-category="' + category.key + '"]');
      if (!section) continue;
      const requests = category.requests || [];
      if (Number(section.dataset.requestCount) !== requests.length) changed = true;
      for (const item of requests) {
        const row = screen.querySelector('[data-request-row="' + item.identity + '"]');
        if (!row) continue;
        setText(row.querySelector('[data-request-status]'), item.status_label);
        setText(row.querySelector('[data-request-detail]'), item.detail);
        const checked = row.querySelector('[data-request-checked]');
        const at = Date.parse(item.last_checked_at);
        if (checked && Number.isFinite(at)) {
          checked.textContent = formatter.format(at);
          checked.parentElement.hidden = false;
        }
        const next = row.querySelector('[data-request-next]');
        const label = item.next_action ? item.next_action.label : '';
        if (next && next.tagName === 'BUTTON') {
          if (item.next_action && next.textContent !== label) next.textContent = label;
        } else if (next) {
          setText(next, label || item.manual_note || '');
        }
      }
    }
    if (changed) location.reload();
  }
  async function refresh() {
    const now = Date.now();
    if (busy || document.hidden || now < nextRead) return;
    busy = true;
    nextRead = now + spacing;
    try {
      const response = await fetch(screen.dataset.requestsStatus, {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        signal: AbortSignal.timeout(8000),
      });
      if (response.status === 429) {
        const retryAfter = Number(response.headers.get('Retry-After'));
        const retryMs = retryAfter > 0 ? Math.min(retryAfter * 1000, interval) : spacing;
        nextRead = Math.max(nextRead, Date.now() + retryMs);
      } else if (response.ok) {
        render(await response.json());
        nextRead = Date.now() + interval;
      }
    } catch {
      nextRead = Date.now() + interval;
    } finally {
      busy = false;
    }
  }
  setInterval(refresh, 10_000);
  document.addEventListener('visibilitychange', refresh);
})();
