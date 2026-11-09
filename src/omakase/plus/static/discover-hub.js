(() => {
  'use strict';
  for (const region of document.querySelectorAll('[data-hub-rows]')) {
    const kind = region.dataset.hubRows;
    fetch('/plus/discover/rows?kind=' + encodeURIComponent(kind), {
      credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(12000),
    }).then(async response => {
      if (!response.ok || response.redirected) throw new Error('Recommendations unavailable');
      region.innerHTML = await response.text();
      document.dispatchEvent(new CustomEvent('omakase:rows-ready', { detail: region }));
    }).catch(() => {
      const notice = document.createElement('p');
      notice.className = 'product-notice';
      notice.setAttribute('role', 'status');
      notice.textContent = kind === 'anime' ? 'Anime recommendations are temporarily unavailable.' : 'Movie and TV recommendations are temporarily unavailable.';
      region.replaceChildren(notice);
    }).finally(() => region.setAttribute('aria-busy', 'false'));
  }
})();
