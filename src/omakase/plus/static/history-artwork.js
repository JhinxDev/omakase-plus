/* Missing exact episode imagery stays an honest episode placeholder. */
(() => {
  'use strict';
  document.querySelectorAll('[data-history-artwork]').forEach((image) => {
    const placeholder = image.parentElement.querySelector('[data-history-placeholder]');
    const unavailable = () => {
      image.hidden = true;
      image.dataset.artworkScope = 'unavailable';
      if (placeholder) placeholder.hidden = false;
    };
    image.dataset.artworkScope = 'episode';
    image.addEventListener('error', unavailable, { once: true });
    if (image.complete && !image.naturalWidth) unavailable();
  });
})();
