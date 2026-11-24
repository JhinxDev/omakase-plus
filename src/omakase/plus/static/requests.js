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
        if (!row || !section.contains(row)) {
          changed = true;
          continue;
        }
        const statusNode = row.querySelector('[data-request-status]');
        setText(statusNode, item.display_label || item.status_label);
        if (statusNode && item.display_tone) {
          statusNode.dataset.statusTone = item.display_tone;
        }
        setText(row.querySelector('[data-request-detail]'), item.display_detail || item.detail);

        const progressWrap = row.querySelector('[data-request-progress]');
        if (progressWrap) {
          if (item.progress && item.progress.expected > 0) {
            progressWrap.hidden = false;
            const fill = progressWrap.querySelector('[data-request-progress-fill]');
            const bar = progressWrap.querySelector('[role="progressbar"]');
            const txt = progressWrap.querySelector('[data-request-progress-text]');
            if (fill) fill.style.width = item.progress.percent + '%';
            if (bar) {
              bar.setAttribute('aria-valuemax', String(item.progress.expected));
              bar.setAttribute('aria-valuenow', String(item.progress.ready));
            }
            setText(
              txt,
              item.progress.ready + ' / ' + item.progress.expected + ' episodes · ' + item.progress.percent + '%',
            );
          } else {
            progressWrap.hidden = true;
          }
        }

        const sublineWrap = row.querySelector('[data-request-subline-wrap]');
        const subline = row.querySelector('[data-request-subline]');
        const subtext = typeof item.display_subline === 'string' ? item.display_subline : '';
        if (sublineWrap && subline) {
          setText(subline, subtext);
          sublineWrap.hidden = !subtext;
        }

        const checked = row.querySelector('[data-request-checked]');
        const at = Date.parse(item.last_checked_at);
        if (checked && Number.isFinite(at)) {
          checked.textContent = formatter.format(at);
          checked.parentElement.hidden = false;
        }
        const next = row.querySelector('[data-request-next]');
        const label = item.next_action ? item.next_action.label : '';
        if (next && next.tagName === 'BUTTON') {
          const buttonText = next.firstChild;
          if (item.next_action && buttonText?.nodeType === Node.TEXT_NODE && buttonText.nodeValue !== label) {
            buttonText.nodeValue = label;
          }
        } else if (next) {
          setText(next, label || item.manual_note || '');
        }
      }
    }
    if (changed && !screen.contains(document.activeElement)) location.reload();
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
