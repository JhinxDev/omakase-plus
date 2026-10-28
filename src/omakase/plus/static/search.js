/* Authenticated, same-origin catalog autocomplete. No query is interpreted as HTML. */
(() => {
  'use strict';
  document.querySelectorAll('[data-catalog-search]').forEach((form, index) => {
    const input = form.querySelector('[data-search-input]');
    const kind = form.querySelector('[data-search-kind]');
    const list = form.querySelector('[data-search-suggestions]');
    const status = form.querySelector('[data-search-status]');
    if (!input || !kind || !list || !status) return;
    list.id ||= `catalog-suggestions-${index}`;
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', list.id);
    input.setAttribute('aria-expanded', 'false');
    let timer, controller, sequence = 0, selected = -1, options = [];
    const announce = message => { status.textContent = message; status.hidden = !message; };
    const close = () => {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      selected = -1;
    };
    const select = next => {
      if (!options.length) return;
      selected = (next + options.length) % options.length;
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      options.forEach((option, i) => option.setAttribute('aria-selected', String(i === selected)));
      input.setAttribute('aria-activedescendant', options[selected].id);
      options[selected].scrollIntoView({ block: 'nearest' });
    };
    const safeUrl = (value, image = false) => {
      if (typeof value !== 'string' || !value.startsWith('/plus/')) return null;
      const parsed = new URL(value, location.origin);
      if (parsed.origin !== location.origin) return null;
      if (image || /^\/plus\/(catalog\/anime\/\d+|movies\/tt\d+|tv\/\d+)(?:\?|$)/.test(parsed.pathname + parsed.search)) return parsed.pathname + parsed.search;
      return null;
    };
    const render = (results, warnings = []) => {
      list.replaceChildren();
      options = [];
      selected = -1;
      results.slice(0, 8).forEach((card, i) => {
        const href = safeUrl(card.url);
        if (!href || typeof card.title !== 'string') return;
        const row = document.createElement('li');
        row.setAttribute('role', 'presentation');
        const link = document.createElement('a');
        link.href = href;
        link.id = `${list.id}-${i}`;
        link.tabIndex = -1;
        link.setAttribute('role', 'option');
        link.setAttribute('aria-selected', 'false');
        const imageUrl = safeUrl(card.poster_url, true);
        if (imageUrl) {
          const poster = document.createElement('img');
          poster.src = imageUrl;
          poster.alt = '';
          poster.addEventListener('error', () => { poster.hidden = true; }, { once: true });
          link.append(poster);
        }
        const copy = document.createElement('span');
        const title = document.createElement('strong');
        title.textContent = card.title;
        const facts = document.createElement('small');
        facts.textContent = [{anime: 'Anime', movie: 'Movie', tv: 'TV'}[card.media_type] || '', card.year, card.format, card.library_id ? 'In your library' : '', card.watch_status === 'completed' ? 'Watched' : '', card.rating ? `Your rating ${card.rating}/10` : ''].filter(Boolean).join(' · ');
        copy.append(title, facts);
        link.append(copy);
        row.append(link);
        list.append(row);
        options.push(link);
      });
      list.hidden = !options.length;
      input.setAttribute('aria-expanded', String(options.length > 0));
      const incomplete = warnings.length ? `Search incomplete: ${warnings.map(warning => `${warning.label || 'Catalog'} unavailable${warning.retry_after ? ` (retry in ${warning.retry_after}s)` : ''}`).join('; ')}.` : '';
      if (options.length && incomplete) {
        const warning = document.createElement('li');
        warning.setAttribute('role', 'status');
        warning.textContent = incomplete;
        list.append(warning);
      }
      announce(options.length ? `${options.length} matching titles. Use arrow keys to choose. ${incomplete}` : incomplete || 'No suggestions. Try another title or category.');
    };
    const search = () => {
      clearTimeout(timer);
      if (controller) controller.abort();
      const own = ++sequence;
      close();
      options = [];
      const query = input.value.trim();
      const category = ['all', 'anime', 'movies', 'tv'].includes(kind.value) ? kind.value : 'all';
      if (query.length < 2) { announce(query ? 'Type at least two characters.' : ''); return; }
      timer = setTimeout(async () => {
        controller = new AbortController();
        announce('Finding matching titles…');
        input.setAttribute('aria-busy', 'true');
        try {
          const url = new URL('/plus/search/suggestions', location.origin);
          url.searchParams.set('q', query);
          url.searchParams.set('kind', category);
          const response = await fetch(url, { signal: controller.signal, credentials: 'same-origin', headers: { Accept: 'application/json' } });
          const payload = await response.json();
          if (own !== sequence || query !== input.value.trim() || category !== kind.value) return;
          if (!Array.isArray(payload.results)) throw new Error('invalid');
          if (!response.ok && !payload.warnings?.length) {
            close();
            announce((payload.detail || 'Suggestions are unavailable. Try again shortly.') + (payload.retry_after ? ` Retry in ${payload.retry_after} seconds.` : ''));
            return;
          }
          render(payload.results, Array.isArray(payload.warnings) ? payload.warnings : []);
        } catch (error) {
          if (error.name !== 'AbortError' && own === sequence) { close(); announce('Suggestions are unavailable. Press Search to try the full catalog.'); }
        } finally {
          if (own === sequence) input.removeAttribute('aria-busy');
        }
      }, 250);
    };
    input.addEventListener('input', search);
    kind.addEventListener('change', search);
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') { ++sequence; clearTimeout(timer); if (controller) controller.abort(); close(); announce(''); input.removeAttribute('aria-busy'); }
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && options.length) { event.preventDefault(); select(selected < 0 ? (event.key === 'ArrowDown' ? 0 : options.length - 1) : selected + (event.key === 'ArrowDown' ? 1 : -1)); }
      if (event.key === 'Enter' && selected >= 0 && !list.hidden) { event.preventDefault(); location.assign(options[selected].href); }
    });
    document.addEventListener('pointerdown', event => { if (!form.contains(event.target)) close(); });
    form.addEventListener('focusout', () => requestAnimationFrame(() => { if (!form.contains(document.activeElement)) close(); }));
  });
})();
