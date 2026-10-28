(() => {
  'use strict';
  const root = document.querySelector('[data-notifications]');
  if (!root) return;
  const toggle = root.querySelector('[data-notification-toggle]');
  const panel = root.querySelector('[data-notification-panel]');
  const closeButton = root.querySelector('[data-notification-close]');
  const badge = root.querySelector('[data-notification-badge]');
  const summary = root.querySelector('[data-notification-summary]');
  const content = root.querySelector('[data-notification-content]');
  const feedback = root.querySelector('[data-notification-feedback]');
  const endpoint = '/plus/api/notifications';
  let feed = null;
  let pending = null;
  let failure = false;
  let opened = false;
  let mutationQueue = Promise.resolve();
  let operationGeneration = 0;
  let appliedGeneration = -1;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function internalHref(value, item) {
    if (typeof value !== 'string') return null;
    const patterns = item.kind === 'removal_reminder'
      ? [/^\/plus\/library\/[1-9]\d*\/remove$/]
      : [/^\/plus\/library\/[1-9]\d*#watch-tracking$/, /^\/plus\/movies\/tt\d+#movie-rating$/, /^\/plus\/tv\/[1-9]\d*#tv-history$/];
    return patterns.some(pattern => pattern.test(value)) ? value : null;
  }
  function normalize(data) {
    if (!data || !Array.isArray(data.items) || !Number.isInteger(data.unread_count) || data.unread_count < 0 || data.unread_count > 100) throw new Error('Invalid notification response');
    const items = data.items.slice(0, 20).filter(item => item && typeof item.id === 'string' && item.id.length > 0 && item.id.length <= 300 && ['completed_unrated', 'removal_reminder'].includes(item.kind) && ['anime', 'movie', 'tv'].includes(item.media_type) && typeof item.title === 'string');
    return { items, unread_count: data.unread_count, count_capped: data.count_capped === true };
  }
  async function request(url, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(url, { ...options, credentials: 'same-origin', signal: controller.signal, headers: { Accept: 'application/json', ...options.headers } });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? 'session' : 'request');
      return normalize(await response.json());
    } finally { window.clearTimeout(timeout); }
  }
  function enqueueMutation(item, action, options) {
    const generation = ++operationGeneration;
    const operation = mutationQueue.then(async () => {
      const data = await request(`${endpoint}/${encodeURIComponent(item.id)}/${action}`, options);
      // Serialized writes plus generations prevent stale reads/responses restoring items.
      if (generation >= appliedGeneration) {
        appliedGeneration = generation;
        feed = data;
        updateBadge();
      }
    });
    mutationQueue = operation.catch(() => {}); // One failure must not strand later actions.
    return operation;
  }
  function notice(message) { feedback.textContent = message; feedback.hidden = false; }
  function state(title, message, retry) {
    const box = element('div', 'notification-state');
    box.setAttribute('role', 'status');
    box.append(element('strong', '', title), element('span', '', message));
    if (retry) {
      const button = element('button', 'notification-retry', 'Try again');
      button.type = 'button';
      button.addEventListener('click', () => { failure = false; load(); });
      box.append(element('br'), button);
    }
    content.replaceChildren(box);
  }
  function updateBadge() {
    if (!feed) return; // Unknown/loading/error is never shown as zero unread.
    badge.hidden = feed.unread_count === 0;
    badge.textContent = feed.unread_count > 99 ? '99+' : String(feed.unread_count);
    toggle.setAttribute('aria-label', `Notifications, ${feed.unread_count > 99 ? '99 or more' : feed.unread_count} unread`);
    summary.textContent = feed.count_capped ? 'Latest reminders - more may be available' : `${feed.unread_count} unread`;
  }
  async function mutate(item, action, button) {
    if (button.disabled) return;
    button.disabled = true;
    feedback.hidden = true;
    try {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
      if (!csrf) throw new Error('session');
      await enqueueMutation(item, action, { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
      render();
      if (opened) {
        const replacement = [...content.querySelectorAll('[data-notification-id]')].find(node => node.dataset.notificationId === item.id);
        (replacement?.querySelector('a,button') || content.querySelector('a,button') || closeButton).focus();
      }
    } catch (error) {
      button.disabled = false;
      notice(error.message === 'session' ? 'Your session needs attention. Refresh this page and sign in before changing reminders.' : 'Could not confirm this change. Refresh or try again.');
    }
  }
  function acknowledge(item) {
    if (item.read === true) return;
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    if (!csrf) return; // Navigation still works; a missing token never grants a write.
    void enqueueMutation(item, 'read', { method: 'POST', keepalive: true, headers: { 'X-CSRF-Token': csrf } }).then(() => {
      const row = [...content.querySelectorAll('[data-notification-id]')].find(node => node.dataset.notificationId === item.id);
      if (row) row.dataset.read = 'true';
    }).catch(() => { if (opened) notice('Could not confirm this reminder was marked as read. The title link still works.'); });
  }
  function itemNode(item) {
    const row = element('li', 'notification-item');
    row.dataset.notificationId = item.id;
    row.dataset.read = String(item.read === true);
    const art = element('span', 'notification-art', { anime: 'AN', movie: 'MV', tv: 'TV' }[item.media_type]);
    art.setAttribute('aria-hidden', 'true'); // Backend intentionally supplies no uncached/provider artwork.
    const copy = element('div', 'notification-item-copy');
    copy.append(element('div', 'notification-kind', item.kind === 'completed_unrated' ? 'Finished watching' : 'Media review'), element('h3', '', item.title.slice(0, 250)));
    copy.append(element('p', 'notification-message', typeof item.message === 'string' ? item.message.slice(0, 500) : 'Review your saved title.'));
    const rating = typeof item.rating === 'number' && Number.isFinite(item.rating) && item.rating >= 0 && item.rating <= 10 ? `Your rating ${item.rating}/10` : 'Not rated yet';
    copy.append(element('p', 'notification-rating', rating));
    const actions = element('div', 'notification-item-actions');
    const href = internalHref(item.href, item);
    if (href) {
      const link = element('a', 'notification-primary', item.kind === 'completed_unrated' ? 'Rate title' : 'Review media');
      link.href = href;
      link.setAttribute('aria-label', `${link.textContent}: ${item.title}`);
      link.addEventListener('click', () => acknowledge(item));
      actions.append(link);
    } else copy.append(element('p', 'notification-message', 'Open this title from your library to continue.'));
    const dismiss = element('button', 'notification-dismiss', '\u00d7');
    dismiss.type = 'button';
    dismiss.title = 'Dismiss reminder';
    dismiss.setAttribute('aria-label', `Dismiss ${item.title}`);
    dismiss.addEventListener('click', () => mutate(item, 'dismiss', dismiss));
    actions.append(dismiss);
    copy.append(actions);
    row.append(art, copy);
    return row;
  }
  function render() {
    updateBadge();
    if (!opened) return;
    if (!feed) {
      summary.textContent = failure ? 'Reminders unavailable' : 'Checking your reminders';
      state(failure === 'session' ? 'Sign in to view notifications' : failure ? 'Could not load notifications' : 'Loading notifications...', failure === 'session' ? 'Refresh this page and sign in to your own account.' : failure ? 'Check your connection or try again.' : 'Your saved reminders will appear here.', failure);
      return;
    }
    if (!feed.items.length) { state('You are all caught up', 'No saved rating or media reminders right now.'); return; }
    const list = element('ul', 'notification-list');
    list.setAttribute('aria-label', 'Saved notifications');
    list.append(...feed.items.map(itemNode));
    content.replaceChildren(list);
  }
  function load() {
    if (pending || feed || failure) return pending;
    render();
    const generation = operationGeneration;
    pending = request(endpoint).then(data => {
      if (generation === operationGeneration && generation >= appliedGeneration) { feed = data; failure = false; }
    }).catch(error => { if (generation === operationGeneration) failure = error.message === 'session' ? 'session' : true; }).finally(() => { pending = null; render(); });
    return pending;
  }
  function close(returnFocus = true) {
    if (!opened) return;
    opened = false;
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    if (returnFocus) toggle.focus();
  }
  toggle.addEventListener('click', () => {
    if (opened) { close(); return; }
    document.querySelector('[data-account-menu]')?.removeAttribute('open');
    document.querySelector('[data-mobile-browse]')?.removeAttribute('open');
    opened = true;
    panel.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    render();
    closeButton.focus();
    load();
  });
  closeButton.addEventListener('click', () => close());
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && opened) { event.preventDefault(); close(); } });
  document.addEventListener('pointerdown', event => { if (opened && !root.contains(event.target)) close(false); });
  document.addEventListener('focusin', event => { if (opened && !root.contains(event.target)) close(false); });
  window.addEventListener('pagehide', () => close(false));
  // One deferred local-DB read per page; opening reuses it. No polling or providers.
  if ('requestIdleCallback' in window) window.requestIdleCallback(() => load(), { timeout: 1500 });
  else window.setTimeout(() => load(), 700);
})();
