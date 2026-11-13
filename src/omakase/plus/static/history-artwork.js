/* Exact episode stills may fall back to an explicitly labeled series cover. */
(() => {
  'use strict';
  const validSeriesPoster = (path) => /^\/plus\/(?:media\/[1-9][0-9]*\/poster|tv\/[1-9][0-9]*\/artwork)$/.test(path);
  const bindArtwork = (image) => {
    const frame = image.parentElement;
    const isSeriesCard = image.hasAttribute('data-series-artwork');
    const placeholder = frame.querySelector('[data-history-placeholder]');
    const label = (isSeriesCard ? frame : frame.parentElement).querySelector('[data-history-artwork-label]');
    const unavailable = () => {
      const fallback = image.dataset.historyFallback || '';
      if (image.dataset.artworkScope !== 'series' && validSeriesPoster(fallback)) {
        image.dataset.artworkScope = 'series';
        image.src = fallback;
        return;
      }
      image.hidden = true;
      image.dataset.artworkScope = 'unavailable';
      if (placeholder) {
        placeholder.hidden = false;
        if (isSeriesCard) {
          placeholder.removeAttribute('aria-hidden');
          placeholder.querySelector('small')?.removeAttribute('hidden');
        }
      }
      if (label) label.hidden = true;
    };
    const loaded = () => {
      if (isSeriesCard && image.naturalWidth < 16) {
        unavailable();
        return;
      }
      if (label) label.hidden = image.dataset.artworkScope !== 'series';
      if (isSeriesCard) {
        image.removeAttribute('data-artwork-pending');
        if (placeholder) placeholder.hidden = true;
      }
    };
    image.addEventListener('load', loaded);
    image.addEventListener('error', unavailable);
    if (image.complete) {
      if (!image.naturalWidth) unavailable();
      else loaded();
    }
  };
  document.querySelectorAll('[data-history-artwork]').forEach(bindArtwork);

  document.querySelectorAll('[data-series-backdrop]').forEach((image) => {
    const unavailable = () => {
      const fallback = image.dataset.seriesBackdrop;
      if (!image.parentElement.classList.contains('series-backdrop--poster') && validSeriesPoster(fallback)) {
        image.parentElement.classList.add('series-backdrop--poster');
        image.src = fallback;
      } else {
        image.hidden = true;
      }
    };
    const loaded = () => {
      if (image.naturalWidth < 16) unavailable();
    };
    image.addEventListener('error', unavailable);
    image.addEventListener('load', loaded);
    if (image.complete) loaded();
  });

  const episodeList = document.querySelector('[data-series-episodes]');
  let episodeMetadata;
  const loadArtwork = async (image) => {
    let still = image.dataset.episodeStill;
    if (episodeList?.dataset.seriesEpisodes) {
      episodeMetadata ||= fetch(episodeList.dataset.seriesEpisodes, {
        mode: 'same-origin',
        signal: AbortSignal.timeout(9000),
      }).then((response) => response.ok ? response.json() : {}).catch(() => ({}));
      const episodes = await episodeMetadata;
      const row = image.closest('[data-episode]');
      const episode = episodes[row?.dataset.episode];
      const heading = row?.querySelector('[data-episode-title]');
      if (heading && typeof episode?.title === 'string' && episode.title) {
        heading.textContent = episode.title;
      }
      if (!episode?.artwork) still = '';
    }
    const fallback = image.dataset.historyFallback;
    if (!still && !fallback) {
      bindArtwork(image);
      return;
    }
    image.dataset.artworkScope = still ? 'episode' : 'series';
    image.setAttribute('data-artwork-pending', '');
    image.hidden = false;
    image.src = still || fallback;
    bindArtwork(image);
  };
  const seriesImages = document.querySelectorAll('[data-series-artwork]');
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        observer.unobserve(entry.target);
        void loadArtwork(entry.target.querySelector('[data-series-artwork]'));
      }
    }, {rootMargin: '200px'});
    seriesImages.forEach((image) => observer.observe(image.parentElement));
  } else {
    seriesImages.forEach(loadArtwork);
  }
})();
