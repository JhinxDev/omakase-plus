/* Authenticated, same-origin catalog autocomplete. No query is interpreted as HTML. */
(() => {
  'use strict';
  document.querySelectorAll('[data-catalog-search]').forEach((form, index) => {
    const input = form.querySelector('[data-search-input]');
    const kind = form.querySelector('[data-search-kind]');
    const list = form.querySelector('[data-search-suggestions]');
    const status = form.querySelector('[data-search-status]');
    const announcement = form.querySelector('[data-search-announcement]');
    if (!input || !kind || !list || !status || !announcement) return;
    list.id ||= `catalog-suggestions-${index}`;
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', list.id);
    input.setAttribute('aria-expanded', 'false');
    let timer, controller, sequence = 0, selected = -1, options = [], cached = null;
    const announce = message => { announcement.textContent = message; };
    const closeVisual = () => {
      list.hidden = true;
      status.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      selected = -1;
    };
    const cancel = () => {
      clearTimeout(timer);
      if (controller) controller.abort();
      controller = null;
      ++sequence;
      input.removeAttribute('aria-busy');
    };
    const dismiss = () => { cancel(); closeVisual(); announce(''); };
    const message = text => {
      status.textContent = text;
      status.hidden = !text;
      announce(text);
      fitPopup();
    };
    const category = () => ['all', 'anime', 'movies', 'tv'].includes(kind.value) ? kind.value : 'all';
    const inside = () => form.contains(document.activeElement);
    let edgeFrame = 0;
    const resultEdges = () => {
      if (list.hidden) return;
      const bounds = list.getBoundingClientRect(), style = getComputedStyle(list);
      const top = bounds.top + list.clientTop + parseFloat(style.paddingTop);
      const bottom = bounds.top + list.clientTop + list.clientHeight - parseFloat(style.paddingBottom);
      options.forEach(option => {
        const parts = [...option.children].map(child => child.getBoundingClientRect());
        const inkTop = Math.min(...parts.map(part => part.top));
        const inkBottom = Math.max(...parts.map(part => part.bottom));
        const intersects = inkBottom > top && inkTop < bottom;
        const partial = intersects && inkBottom - inkTop <= bottom - top && (inkTop < top - 1 || inkBottom > bottom + 1);
        if (partial) option.dataset.clippedEdge = inkTop < top ? 'start' : 'end';
        else delete option.dataset.clippedEdge;
      });
    };
    const fitPopup = () => {
      if (list.hidden && status.hidden) return;
      const field = input.getBoundingClientRect(), viewport = window.visualViewport;
      const viewportTop = viewport ? viewport.offsetTop : 0;
      let bottom = viewportTop + (viewport ? viewport.height : innerHeight);
      const navigation = document.querySelector('.mobile-primary-nav');
      if (navigation && getComputedStyle(navigation).position === 'fixed') {
        const nav = navigation.getBoundingClientRect();
        if (nav.height) bottom = Math.min(bottom, nav.top);
      }
      const below = bottom - field.bottom - 20, above = field.top - viewportTop - 20;
      const opensAbove = below < 160 && above > below;
      [list, status].forEach(popup => {
        popup.classList.toggle('opens-above', opensAbove);
        popup.style.maxHeight = `${Math.max(1, Math.min(480, opensAbove ? above : below))}px`;
      });
      resultEdges();
    };
    list.addEventListener('scroll', () => {
      if (!edgeFrame) edgeFrame = requestAnimationFrame(() => { edgeFrame = 0; resultEdges(); });
    }, { passive: true });
    window.addEventListener('resize', fitPopup);
    window.addEventListener('scroll', fitPopup, { passive: true });
    window.visualViewport?.addEventListener('resize', fitPopup);
    const select = next => {
      if (!options.length) return;
      selected = (next + options.length) % options.length;
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      options.forEach((option, i) => option.setAttribute('aria-selected', String(i === selected)));
      input.setAttribute('aria-activedescendant', options[selected].id);
      options[selected].scrollIntoView({ block: 'nearest' });
      resultEdges();
    };
    const safeUrl = (value, image = false) => {
      if (typeof value !== 'string' || !value.startsWith('/plus/')) return null;
      const parsed = new URL(value, location.origin);
      if (parsed.origin !== location.origin) return null;
      if (image) return parsed.pathname + parsed.search;
      if (/^\/plus\/(catalog\/anime\/\d+|library\/\d+|libraries\/\d+\/items\/\d+|movies\/tt\d+|tv\/\d+)$/.test(parsed.pathname) && (!parsed.hash || /^#(overview|episode-heading|episodes|episode-[1-9]\d{0,5})$/.test(parsed.hash))) return parsed.pathname + parsed.search + parsed.hash;
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
      status.hidden = true;
      input.setAttribute('aria-expanded', String(options.length > 0));
      input.removeAttribute('aria-activedescendant');
      fitPopup();
      const incomplete = warnings.length ? `Search incomplete: ${warnings.map(warning => `${warning.label || 'Catalog'} unavailable${warning.retry_after ? ` (retry in ${warning.retry_after}s)` : ''}`).join('; ')}.` : '';
      if (options.length && incomplete) {
        const warning = document.createElement('li');
        warning.setAttribute('role', 'presentation');
        warning.textContent = incomplete;
        list.append(warning);
      }
      if (options.length) announce(`${options.length} matching titles. Use arrow keys to choose. ${incomplete}`);
      else message(incomplete || 'No suggestions. Try another title or category.');
    };
    const search = () => {
      cancel();
      const own = sequence;
      closeVisual();
      announce('');
      options = [];
      const query = input.value.trim();
      const selectedCategory = category();
      if (!inside()) return;
      if (query.length < 2) { message(query ? 'Type at least two characters.' : ''); return; }
      if (cached && cached.expires > performance.now() && cached.query === query && cached.category === selectedCategory) {
        render(cached.results, cached.warnings);
        return;
      }
      cached = null;
      timer = setTimeout(async () => {
        if (own !== sequence || !inside()) return;
        controller = new AbortController();
        message('Finding matching titles…');
        input.setAttribute('aria-busy', 'true');
        try {
          const url = new URL('/plus/search/suggestions', location.origin);
          url.searchParams.set('q', query);
          url.searchParams.set('kind', selectedCategory);
          const response = await fetch(url, { signal: controller.signal, credentials: 'same-origin', headers: { Accept: 'application/json' } });
          const payload = await response.json();
          if (own !== sequence || !inside() || query !== input.value.trim() || selectedCategory !== category()) return;
          if (!Array.isArray(payload.results)) throw new Error('invalid');
          if (!response.ok && !payload.warnings?.length) {
            closeVisual();
            message((payload.detail || 'Suggestions are unavailable. Try again shortly.') + (payload.retry_after ? ` Retry in ${payload.retry_after} seconds.` : ''));
            return;
          }
          const results = payload.results.slice(0, 8);
          const warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
          if (response.ok && results.length && !warnings.length) cached = { query, category: selectedCategory, results, warnings, expires: performance.now() + 30_000 };
          render(results, warnings);
        } catch (error) {
          if (error.name !== 'AbortError' && own === sequence && inside()) { closeVisual(); message('Suggestions are unavailable. Press Search to try the full catalog.'); }
        } finally {
          if (own === sequence) input.removeAttribute('aria-busy');
        }
      }, 250);
    };
    input.addEventListener('input', search);
    input.addEventListener('focus', search);
    kind.addEventListener('change', search);
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') dismiss();
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && options.length) { event.preventDefault(); select(selected < 0 ? (event.key === 'ArrowDown' ? 0 : options.length - 1) : selected + (event.key === 'ArrowDown' ? 1 : -1)); }
      if (event.key === 'Enter' && selected >= 0 && !list.hidden) { event.preventDefault(); location.assign(options[selected].href); }
    });
    document.addEventListener('pointerdown', event => { if (!form.contains(event.target)) dismiss(); });
    form.addEventListener('focusout', () => requestAnimationFrame(() => { if (!inside()) dismiss(); }));
  });
})();
