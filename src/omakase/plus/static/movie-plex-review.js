(() => {
  'use strict';
  const form = document.querySelector('[data-movie-plex-review]');
  if (!form) return;
  const status = document.querySelector('[data-movie-plex-result]');
  const button = form.querySelector('button');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    status.textContent = 'Checking Plex…';
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new URLSearchParams(new FormData(form)), credentials: 'same-origin',
        headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(25000),
      });
      if (!response.ok || response.redirected) throw new Error('Plex check failed');
      const {result, plex_url: url, cleanup} = await response.json();
      if (result === 'matched') {
        const target = new URL(url);
        if (target.origin !== 'https://app.plex.tv') throw new Error('Unexpected Plex destination');
        const link = document.createElement('a');
        link.href = target.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.className = 'button button-secondary';
        link.textContent = 'Manage copy in Plex';
        const messages = {
          verified: 'The Plex copy and its Real-Debrid torrent match.',
          protected: 'The Plex and Real-Debrid copies match. This copy is protected from automatic cleanup.',
          unverified: 'This movie is in Plex. The exact Real-Debrid copy could not be verified.',
        };
        const message = Object.hasOwn(messages, cleanup) ? messages[cleanup] : 'This movie is in Plex.';
        status.replaceChildren(message, link);
      } else {
        status.textContent = result === 'absent'
          ? 'No matching copy in your Plex Movies library.'
          : 'Plex could not confirm this copy. Try again.';
      }
    } catch {
      status.textContent = 'Plex could not confirm this copy. Try again.';
    } finally {
      button.disabled = false;
    }
  });
})();
