/* Keep a title-list position across a detail visit.
 *
 * A list page (For You tab, Movies, TV Shows, Search, Library, Home) records its
 * own context when a title link is followed: path and query, vertical scroll,
 * horizontal carousel offsets, the opened title and the active tab. The detail
 * page sends Back through history, and the list page re-applies that context.
 *
 * Storage is sessionStorage, so it is per browser tab and per visit. A direct
 * link has no record, or no return navigation: nothing is restored, the page
 * stays at the normal top and the detail page uses its own plain back URL.
 */
(() => {
  'use strict';

  const RECORD_KEY = 'omakase.nav.origin';
  const RETURN_KEY = 'omakase.nav.return';
  const MAX_AGE_MS = 30 * 60 * 1000;
  const RAIL_SELECTOR = '[data-nav-scroll], .media-rail';
  const APPLY_TICKS_MS = [0, 60, 180, 480, 1000, 1800, 2800];
  let lastInputKeyboard = false;

  document.addEventListener('pointerdown', () => { lastInputKeyboard = false; }, true);
  document.addEventListener('keydown', event => {
    if (!event.metaKey && !event.ctrlKey && !event.altKey) lastInputKeyboard = true;
  }, true);

  let storage = null;
  try {
    const probe = '__omakase_nav_probe__';
    window.sessionStorage.setItem(probe, '1');
    window.sessionStorage.removeItem(probe);
    storage = window.sessionStorage;
  } catch (error) {
    storage = null;
  }

  const here = () => window.location.pathname + window.location.search;

  function read(key) {
    if (!storage) return null;
    try {
      const raw = storage.getItem(key);
      if (!raw) return null;
      const value = JSON.parse(raw);
      if (!value || typeof value.path !== 'string') return null;
      if (typeof value.ts !== 'number' || Date.now() - value.ts > MAX_AGE_MS) return null;
      return value;
    } catch (error) {
      return null;
    }
  }

  function write(key, value) {
    if (!storage) return;
    try {
      storage.setItem(key, JSON.stringify(value));
    } catch (error) {
      /* A full or disabled store simply means no restore. */
    }
  }

  function drop(key) {
    if (!storage) return;
    try {
      storage.removeItem(key);
    } catch (error) {
      /* ignore */
    }
  }

  function originRecord() {
    const record = read(RECORD_KEY);
    if (!record || record.v !== 1) return null;
    return record;
  }

  function currentNavigationType() {
    try {
      const entry = window.performance.getEntriesByType('navigation')[0];
      return entry && entry.type ? entry.type : '';
    } catch (error) {
      return '';
    }
  }

  function referrerPath() {
    try {
      const url = new URL(document.referrer);
      if (url.origin !== window.location.origin) return '';
      return url.pathname + url.search;
    } catch (error) {
      return '';
    }
  }

  function railKey(rail, index) {
    if (rail.id) return rail.id;
    const label = rail.getAttribute('aria-label');
    if (label) return label;
    return 'rail-' + index;
  }

  function rails() {
    return [...document.querySelectorAll(RAIL_SELECTOR)].filter(
      rail => !rail.closest('[aria-hidden="true"]')
    );
  }

  function activeTab() {
    const tab = document.querySelector(
      '.hub-divisions [aria-current="page"], .movie-tabs [aria-current="page"],' +
        ' .language-tabs [aria-current="page"]'
    );
    return tab ? (tab.textContent || '').trim().slice(0, 60) : '';
  }

  function focusTarget(record) {
    if (!record.focusHref) return null;
    const findLink = root => [...root.querySelectorAll('a[href]')].find(
      node => node.getAttribute('href') === record.focusHref && node.getClientRects().length > 0
    );
    const rail = record.focusRail && document.getElementById(record.focusRail);
    if (rail) {
      const card = [...rail.querySelectorAll('[data-card-id]')]
        .find(item => item.dataset.cardId === record.focusCard);
      if (card) return findLink(card) || card.querySelector('.om-card-link');
      return findLink(rail) || findLink(document);
    }
    return findLink(document);
  }

  function capture(anchor, viaKeyboard) {
    const scrollY = Math.max(0, Math.round(window.scrollY || 0));
    const rows = {};
    rails().forEach((rail, index) => {
      if (rail.scrollWidth - rail.clientWidth > 2 && rail.scrollLeft > 0) {
        rows[railKey(rail, index)] = Math.round(rail.scrollLeft);
      }
    });
    const rail = anchor.closest('[data-nav-scroll][id]');
    return {
      v: 1,
      path: here(),
      tab: activeTab(),
      scrollY,
      rows,
      viaKeyboard: Boolean(viaKeyboard),
      focusHref: anchor ? anchor.getAttribute('href') || '' : '',
      focusRail: rail ? rail.id : '',
      focusCard: anchor.closest('[data-card-id]')?.dataset.cardId || '',
      focusLabel: anchor ? (anchor.textContent || '').trim().slice(0, 120) : '',
      ts: Date.now(),
    };
  }

  function restoreHorizontal(record) {
    const rows = record.rows || {};
    rails().forEach((rail, index) => {
      const target = rows[railKey(rail, index)];
      if (typeof target === 'number' && target > 0 && rail.scrollLeft !== target) {
        rail.scrollLeft = target;
      }
    });
  }

  function restoreVertical(record) {
    const target = Math.max(0, Math.round(record.scrollY || 0));
    if (target > 0 && Math.abs(Math.round(window.scrollY || 0) - target) > 2) {
      window.scrollTo(0, target);
    }
  }

  /* Applies the record at a few layout milestones: For You rows arrive by fetch
     after first paint, so one early scroll would land short. The ticks are finite
     and stop on pointer, wheel or key input, so a reader is never fought. */
  function restore(record) {
    let cancelled = false;
    let focused = false;
    const cancel = () => {
      cancelled = true;
    };
    ['pointerdown', 'wheel', 'touchstart', 'keydown'].forEach(name => {
      window.addEventListener(name, cancel, { once: true, passive: true });
    });
    const apply = () => {
      if (cancelled) return;
      restoreVertical(record);
      restoreHorizontal(record);
      if (record.viaKeyboard === true) {
        if (!focused) {
          const target = focusTarget(record);
          if (target) {
            try {
              target.focus({ preventScroll: true });
            } catch (error) {
              target.focus();
            }
            focused = true;
          }
        }
      } else if (
        document.activeElement &&
        document.activeElement.matches &&
        document.activeElement.matches('a.hub-card-link, a.om-card-link, .poster-card-link')
      ) {
        document.activeElement.blur();
      }
    };
    APPLY_TICKS_MS.forEach(delay => {
      if (delay === 0) apply();
      else window.setTimeout(apply, delay);
    });
    document.addEventListener('omakase:rows-ready', apply);
    window.addEventListener('load', apply);
  }

  /* A return is browser Back (back_forward), a previously recorded list URL as
     the referrer, or the detail page's own in-page Back navigating here. */
  function returningTo(record) {
    if (currentNavigationType() === 'back_forward') return true;
    if (referrerPath() && referrerPath() === record.path) return true;
    const flag = read(RETURN_KEY);
    if (flag && flag.path === record.path) {
      drop(RETURN_KEY);
      return true;
    }
    return false;
  }

  function restoreIfReturning() {
    const record = originRecord();
    if (!record || record.path !== here()) return false;
    if (!returningTo(record)) return false;
    restore(record);
    return true;
  }

  /* In-page Back: use history when it leads back to the recorded list, otherwise
     navigate to that list. With no usable record the template's href decides. */
  function handleBack(event) {
    const control = event.target instanceof Element ? event.target.closest('[data-nav-back]') : null;
    if (!control || event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const record = originRecord();
    if (!record) return;
    const from = referrerPath();
    if (from && from !== record.path) return;
    event.preventDefault();
    if (from === record.path) {
      window.history.back();
      return;
    }
    if (window.history.length > 1 && currentNavigationType() !== 'navigate') {
      window.history.back();
      return;
    }
    write(RETURN_KEY, { path: record.path, ts: Date.now() });
    window.location.assign(record.path);
  }

  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!anchor) return;
    if (anchor.closest('[data-nav-back]') || anchor.hasAttribute('download')) return;
    const target = anchor.getAttribute('target');
    if (target && target !== '_self') return;
    let url;
    try {
      url = new URL(anchor.href, window.location.href);
    } catch (error) {
      return;
    }
    if (url.origin !== window.location.origin) return;
    if (url.pathname + url.search === here()) return;
    write(RECORD_KEY, capture(anchor, lastInputKeyboard || event.detail === 0));
  });

  document.addEventListener('click', handleBack);

  window.addEventListener('pageshow', event => {
    if (event.persisted) restoreIfReturning();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restoreIfReturning, { once: true });
  } else {
    restoreIfReturning();
  }
})();
