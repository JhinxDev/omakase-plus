/* Exact episode stills may fall back to an explicitly labeled series cover. */
(() => {
  'use strict';
  document.querySelectorAll('[data-history-artwork]').forEach((image) => {
    const frame = image.parentElement;
    const placeholder = frame.querySelector('[data-history-placeholder]');
    const label = frame.parentElement.querySelector('[data-history-artwork-label]');
    const unavailable = () => {
      const fallback = image.dataset.historyFallback || '';
      if (image.dataset.artworkScope !== 'series' && /^\/plus\/(?:media\/[1-9][0-9]*\/poster|tv\/[1-9][0-9]*\/artwork)$/.test(fallback)) {
        image.dataset.artworkScope = 'series';
        image.src = fallback;
        return;
      }
      image.hidden = true;
      image.dataset.artworkScope = 'unavailable';
      if (placeholder) placeholder.hidden = false;
      if (label) label.hidden = true;
    };
    image.addEventListener('load', () => {
      if (label) label.hidden = image.dataset.artworkScope !== 'series';
    });
    image.addEventListener('error', unavailable);
    if (image.complete) {
      if (!image.naturalWidth) unavailable();
      else if (label) label.hidden = image.dataset.artworkScope !== 'series';
    }
  });
})();
