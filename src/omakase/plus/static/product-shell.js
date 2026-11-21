/* Omakase Plus shared product behaviour.
 *
 * Shared Home, artwork and navigation controls:
 *   1. Edit Home: reorder, hide and add shelves in the dialog, then send one
 *      ordered submission. Servers still receive a plain form, so the same
 *      controls work with scripting disabled.
 *   2. Artwork: when a wide image fails, keep the card layout honest by
 *      falling back to the poster, then to the neutral gradient tile.
 */
(function () {
  'use strict';

  var MISSING = 'is-artwork-missing';
  var dimArtworkKey = 'omakase.dim-artwork';
  var dimArtwork = false;
  try {
    dimArtwork = window.localStorage.getItem(dimArtworkKey) === '1';
  } catch (_) { /* A private browser may block local storage. */ }
  document.documentElement.toggleAttribute('data-dim-artwork', dimArtwork);

  function wireArtworkPreference() {
    var option = document.querySelector('[data-dim-artwork-option]');
    var toggle = option?.querySelector('input');
    if (!toggle) return;
    option.hidden = false;
    toggle.checked = dimArtwork;
    toggle.addEventListener('change', function () {
      document.documentElement.toggleAttribute('data-dim-artwork', toggle.checked);
      try {
        window.localStorage.setItem(dimArtworkKey, toggle.checked ? '1' : '0');
      } catch (_) { /* The switch still works for this page. */ }
    });
  }

  function rowsField(form) {
    return form.querySelector('[data-edit-home-order]');
  }

  function rowItems(form) {
    var list = form.querySelector('[data-edit-home-rows]');
    return list ? Array.prototype.slice.call(list.querySelectorAll('[data-row-id]')) : [];
  }

  function syncOrder(form) {
    var field = rowsField(form);
    if (!field) return;
    var rows = rowItems(form);
    field.value = rows
      .map(function (item) {
        return item.getAttribute('data-row-id');
      })
      .join(',');
    // Hidden shelves travel as repeated hidden inputs so the same submission
    // works with and without scripting.
    Array.prototype.slice
      .call(form.querySelectorAll('input[data-hidden-row]'))
      .forEach(function (input) {
        input.remove();
      });
    rows.forEach(function (item) {
      if (!item.classList.contains('is-hidden')) return;
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'hidden';
      input.value = item.getAttribute('data-row-id');
      input.setAttribute('data-hidden-row', '');
      form.appendChild(input);
    });
    rows.forEach(function (item, index) {
      var up = item.querySelector('button[value="move-up"]');
      var down = item.querySelector('button[value="move-down"]');
      if (up) up.disabled = index === 0;
      if (down) down.disabled = index === rows.length - 1;
    });
  }

  function setHidden(item, hidden) {
    item.classList.toggle('is-hidden', hidden);
    var name = item.querySelector('.edit-home-name');
    if (name) {
      var note = name.querySelector('small');
      if (hidden && !note) {
        note = document.createElement('small');
        note.textContent = 'Hidden';
        name.appendChild(note);
      } else if (!hidden && note) {
        note.remove();
      }
    }
    var remove = item.querySelector('button[value="remove"]');
    var add = item.querySelector('button[value="add"]');
    if (remove) remove.hidden = hidden;
    if (add) add.hidden = !hidden;
  }

  function wireEditHome(dialog) {
    var form = dialog.querySelector('[data-edit-home-form]');
    if (!form) return;
    var close = dialog.querySelector('[data-edit-home-close]');

    form.addEventListener('submit', function (event) {
      var submitter = event.submitter;
      var value = submitter ? submitter.value : 'save';
      if (value === 'move-up' || value === 'move-down' || value === 'remove' || value === 'add') {
        event.preventDefault();
        var item = submitter.closest('[data-row-id]');
        if (!item) return;
        if (value === 'move-up' && item.previousElementSibling) {
          item.parentNode.insertBefore(item, item.previousElementSibling);
        } else if (value === 'move-down' && item.nextElementSibling) {
          item.parentNode.insertBefore(item.nextElementSibling, item);
        } else if (value === 'remove') {
          setHidden(item, true);
        } else if (value === 'add') {
          setHidden(item, false);
        }
        syncOrder(form);
      }
    });

    if (close) {
      close.addEventListener('click', function () {
        if (typeof dialog.close === 'function') dialog.close();
        else dialog.removeAttribute('open');
      });
    }
    dialog.addEventListener('click', function (event) {
      if (event.target === dialog && typeof dialog.close === 'function') dialog.close();
    });
    syncOrder(form);
  }

  function openEditHome() {
    var dialog = document.querySelector('[data-edit-home]');
    if (!dialog) return;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    var first = dialog.querySelector('.edit-home-row button:not(:disabled):not([hidden])');
    if (first) first.focus({ preventScroll: true });
  }

  function wireOpeners() {
    var openers = document.querySelectorAll('[data-edit-home-open]');
    Array.prototype.forEach.call(openers, function (button) {
      button.addEventListener('click', function (event) {
        event.preventDefault();
        openEditHome();
      });
    });
  }

  function wireArtwork() {
    document.addEventListener(
      'error',
      function (event) {
        var image = event.target;
        if (!image || image.tagName !== 'IMG' || !image.hasAttribute('data-artwork')) return;
        if (image.getAttribute('data-artwork') === 'done') return;
        var poster = image.getAttribute('data-poster');
        if (poster && image.getAttribute('data-fallback') === 'poster' && image.src !== poster) {
          image.setAttribute('data-fallback', 'none');
          image.src = poster;
          return;
        }
        image.setAttribute('data-artwork', 'done');
        var cover = image.closest('.om-card-cover');
        if (cover && !cover.querySelector('[data-artwork-missing]')) {
          var tile = document.createElement('span');
          tile.className = 'om-card-art om-card-art--empty';
          tile.setAttribute('data-artwork-missing', '');
          tile.setAttribute('aria-hidden', 'true');
          image.replaceWith(tile);
        }
        var banner = image.closest('[data-home-banner]');
        if (banner) banner.classList.add('home-banner--no-art');
      },
      true
    );
  }

  function wireMobilePanels() {
    var panels = document.querySelectorAll('details[data-mobile-browse], details[data-mobile-more]');
    panels.forEach(function (panel) {
      panel.addEventListener('keydown', function (event) {
        if (event.key !== 'Escape' || !panel.open) return;
        event.preventDefault();
        panel.open = false;
        panel.querySelector('summary').focus();
      });
    });
    document.addEventListener('click', function (event) {
      Array.prototype.forEach.call(panels, function (panel) {
        if (!panel.open) return;
        if (!panel.contains(event.target)) panel.open = false;
      });
    });
  }

  function wireHomeFeature() {
    var feature = document.querySelector('[data-home-feature]');
    var controls = feature?.querySelector('[data-home-feature-controls]');
    if (!controls) return;
    var panels = Array.from(feature.querySelectorAll('[data-home-feature-panel]'));
    var pause = controls.querySelector('[data-feature-pause]');
    var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    var motionKey = 'omakase.home-feature-motion';
    var current = panels.findIndex(function (panel) {
      return panel.dataset.homeFeaturePanel === feature.querySelector('[data-home-banner]').dataset.bannerDomain;
    });
    var preference = null;
    try {
      var saved = localStorage.getItem(motionKey);
      if (saved === 'paused' || saved === 'playing') preference = saved;
    } catch (_) { /* Storage may be disabled for this browsing session. */ }
    function isStopped() { return preference === null ? motion.matches : preference === 'paused'; }
    var timer;
    function updatePause() {
      var stopped = isStopped();
      var label = stopped ? 'Play featured titles' : 'Pause featured titles';
      pause.dataset.paused = String(stopped);
      pause.setAttribute('aria-label', label);
      pause.title = label;
    }
    function advance(step) {
      current = (current + step + panels.length) % panels.length;
      feature.querySelector('[data-home-banner]').replaceWith(panels[current].content.cloneNode(true));
      document.dispatchEvent(new Event('omakase:hero-replaced'));
    }
    function schedule() {
      window.clearTimeout(timer);
      if (isStopped() || document.hidden || feature.matches(':hover, :focus-within')) return;
      timer = window.setTimeout(function () { advance(1); schedule(); }, 8000);
    }
    controls.hidden = false;
    updatePause();
    controls.addEventListener('click', function (event) {
      var step = event.target.closest('[data-feature-step]');
      if (step) advance(Number(step.dataset.featureStep));
      if (pause.contains(event.target)) {
        preference = isStopped() ? 'playing' : 'paused';
        try { localStorage.setItem(motionKey, preference); } catch (_) { /* Keep the page setting. */ }
        updatePause();
      }
      schedule();
    });
    ['pointerenter', 'pointerleave', 'focusin'].forEach(function (name) { feature.addEventListener(name, schedule); });
    feature.addEventListener('focusout', function () { window.setTimeout(schedule, 0); });
    document.addEventListener('visibilitychange', schedule);
    motion.addEventListener('change', function () { updatePause(); schedule(); });
    schedule();
  }

  function wireHomeCardMenus() {
    var opened = null;
    function closeMenu(menu, restoreFocus) {
      var panel = menu.querySelector('.om-card-menu-panel');
      if (panel.matches(':popover-open')) panel.hidePopover();
      menu.open = false;
      if (opened === menu) opened = null;
      if (restoreFocus) menu.querySelector('summary').focus({ preventScroll: true });
    }
    document.addEventListener('toggle', function (event) {
      var menu = event.target;
      if (!menu.matches?.('details.om-card-menu')) return;
      var panel = menu.querySelector('.om-card-menu-panel');
      if (typeof panel.showPopover !== 'function') return;
      if (!menu.open) {
        closeMenu(menu, false);
        return;
      }
      if (opened && opened !== menu) closeMenu(opened, false);
      opened = menu;
      panel.setAttribute('popover', 'manual');
      panel.showPopover();
      var anchor = menu.querySelector('summary').getBoundingClientRect();
      var bounds = panel.getBoundingClientRect();
      var below = anchor.bottom + 4;
      var top = below + bounds.height <= window.innerHeight - 8 ? below : anchor.top - bounds.height - 4;
      panel.style.left = Math.max(8, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 8)) + 'px';
      panel.style.top = Math.max(8, top) + 'px';
    }, true);
    document.addEventListener('click', function (event) {
      if (opened && !opened.contains(event.target)) closeMenu(opened, false);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape' || !opened) return;
      event.preventDefault();
      closeMenu(opened, true);
    }, true);
    window.addEventListener('resize', function () { if (opened) closeMenu(opened, false); }, { passive: true });
    document.addEventListener('scroll', function (event) {
      if (opened && !(event.target instanceof Node && opened.contains(event.target))) closeMenu(opened, false);
    }, { passive: true, capture: true });
  }

  function start() {
    var dialog = document.querySelector('[data-edit-home]');
    if (dialog) wireEditHome(dialog);
    wireOpeners();
    wireArtwork();
    wireArtworkPreference();
    wireMobilePanels();
    wireHomeFeature();
    wireHomeCardMenus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
  document.addEventListener('omakase:home-ready', function () {
    var dialog = document.querySelector('[data-edit-home]');
    if (dialog) wireEditHome(dialog);
  });
})();
