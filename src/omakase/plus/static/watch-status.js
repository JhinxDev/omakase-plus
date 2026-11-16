// Owner review W-05: the app's own watch status listbox.
//
// A native <select> popup is drawn by the browser or the OS, so its font, colours and
// hover treatment cannot be styled. This control owns the list and keeps the full
// keyboard contract: Enter/Space/Down opens, Up/Down/Home/End move, Enter or Space
// commits, Escape closes without changing anything, Tab or a click outside closes, and
// the committed value is written into the hidden input the existing form posts.
(function () {
  'use strict';

  const SELECTOR = '[data-watch-status]';

  function parts(root) {
    return {
      trigger: root.querySelector('[data-watch-status-trigger]'),
      list: root.querySelector('[data-watch-status-list]'),
      input: root.querySelector('input[name="status"]'),
      text: root.querySelector('[data-watch-status-text]'),
      options: () => Array.from(root.querySelectorAll('[role="option"]')),
    };
  }

  function setExpanded(p, open) {
    p.list.hidden = !open;
    p.trigger.setAttribute('aria-expanded', String(open));
    if (!open) p.trigger.removeAttribute('aria-activedescendant');
  }

  function select(root, value) {
    const p = parts(root);
    const option = p.options().find(item => item.dataset.value === value);
    if (!option) return;
    root.dataset.value = value;
    p.input.value = value;
    p.text.textContent = option.textContent.trim();
    for (const item of p.options()) {
      item.setAttribute('aria-selected', String(item === option));
    }
    // Announce the change through the form's existing status region, if the page has one.
    root.dispatchEvent(new CustomEvent('watch-status-change', { bubbles: true, detail: { value } }));
  }

  function highlight(root, option) {
    const p = parts(root);
    for (const item of p.options()) item.classList.toggle('is-active', item === option);
    if (!option) {
      p.trigger.removeAttribute('aria-activedescendant');
      return;
    }
    p.trigger.setAttribute('aria-activedescendant', option.id);
    option.scrollIntoView({ block: 'nearest' });
  }

  function currentValue(root) {
    return root.dataset.value || '';
  }

  document.addEventListener('keydown', event => {
    const root = event.target.closest ? event.target.closest(SELECTOR) : null;
    if (!root) return;
    const p = parts(root);
    const open = !p.list.hidden;
    const options = p.options();
    const activeId = p.trigger.getAttribute('aria-activedescendant');
    const index = Math.max(0, options.findIndex(item => item.id === activeId));
    const selectedIndex = Math.max(0, options.findIndex(item => item.dataset.value === currentValue(root)));

    const move = target => {
      const bounded = Math.min(options.length - 1, Math.max(0, target));
      highlight(root, options[bounded]);
    };

    switch (event.key) {
      case 'Enter':
      case ' ':
        event.preventDefault();
        if (!open) {
          setExpanded(p, true);
          highlight(root, options[selectedIndex]);
        } else {
          const chosen = options[index] || options[selectedIndex];
          if (chosen) select(root, chosen.dataset.value);
          setExpanded(p, false);
          p.trigger.focus();
        }
        return;
      case 'ArrowDown':
        event.preventDefault();
        if (!open) {
          setExpanded(p, true);
          highlight(root, options[selectedIndex]);
        } else {
          move(index + 1);
        }
        return;
      case 'ArrowUp':
        event.preventDefault();
        if (!open) {
          setExpanded(p, true);
          highlight(root, options[selectedIndex]);
        } else {
          move(index - 1);
        }
        return;
      case 'Home':
        if (!open) return;
        event.preventDefault();
        move(0);
        return;
      case 'End':
        if (!open) return;
        event.preventDefault();
        move(options.length - 1);
        return;
      case 'Escape':
        if (!open) return;
        event.preventDefault();
        event.stopPropagation();
        setExpanded(p, false);
        p.trigger.focus();
        return;
      case 'Tab':
        if (open) setExpanded(p, false);
        return;
      default:
        return;
    }
  });

  document.addEventListener('click', event => {
    const root = event.target.closest ? event.target.closest(SELECTOR) : null;
    for (const other of document.querySelectorAll(SELECTOR)) {
      if (other === root) continue;
      const p = parts(other);
      if (p.list && !p.list.hidden) setExpanded(p, false);
    }
    if (!root) return;
    const p = parts(root);
    const option = event.target.closest('[role="option"]');
    if (option && root.contains(option)) {
      select(root, option.dataset.value);
      setExpanded(p, false);
      p.trigger.focus();
      return;
    }
    if (event.target.closest('[data-watch-status-trigger]')) {
      const open = p.list.hidden;
      setExpanded(p, open);
      if (open) {
        const options = p.options();
        const selected = options.find(item => item.dataset.value === currentValue(root));
        highlight(root, selected || options[0]);
      }
    }
  });
})();
