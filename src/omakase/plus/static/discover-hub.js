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
      const updatedRequest = new URLSearchParams(location.search).get('request');
      const progress = [...region.querySelectorAll('[data-title-request]')]
        .find(node => node.dataset.titleRequest === updatedRequest);
      if (progress) {
        progress.open = true;
        progress.querySelector('summary')?.focus();
      }
    }).catch(() => {
      const notice = document.createElement('p');
      notice.className = 'product-notice';
      notice.setAttribute('role', 'status');
      notice.textContent = 'Recommendations are temporarily unavailable. Try again in a moment.';
      region.replaceChildren(notice);
    }).finally(() => region.setAttribute('aria-busy', 'false'));
  }
})();
