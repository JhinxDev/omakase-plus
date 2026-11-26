// The app's own listbox, shared by every control that used to be a native <select>.
//
// A native <select> popup is drawn by the browser or the operating system, so its font,
// colours, hover treatment and sizing cannot be reached by author CSS. Owner review W-05
// therefore replaced the watch status <select> with an app-owned listbox, and owner
// decision #6 asks for the same treatment on the TV season picker. This file is that one
// control, used by both, so the keyboard contract only has to be right once.
//
// Keyboard contract: Enter/Space/Down opens, Up/Down/Home/End move, Enter or Space
// commits, Escape closes without changing anything, Tab or a click outside closes, and the
// committed value is written into the hidden input that the surrounding form posts.
//
// Activation model. The trigger is a real <button>, so the browser already turns Enter and
// Space on it into a click. Handling those two keys here as well would open the list on
// keydown and then immediately close it again on the click, so this file deliberately
// leaves Enter and Space on the trigger to the button itself and reads the resulting click.
// A keyboard-activated click carries detail === 0 while a mouse click carries detail >= 1,
// which is what lets Enter commit the highlighted option while a mouse click on the trigger
// merely dismisses the list without reloading the page.
//
// Markup contract, all inside one [data-listbox] root:
//   [data-listbox-trigger]      the button that opens the list
//   [data-listbox-list]         the <ul role="listbox"> holding <li role="option">
//   [data-listbox-text]         the element showing the committed label
//   input[type="hidden"]        the committed value (optional for a navigating control)
//   data-listbox-submit         optional: submit the closest form on commit
//   data-listbox-navigate       optional: URL to visit on commit; {value} is replaced
(function () {
  'use strict';

  const SELECTOR = '[data-listbox]';

  function parts(root) {
    return {
      trigger: root.querySelector('[data-listbox-trigger]'),
      list: root.querySelector('[data-listbox-list]'),
      input: root.querySelector('input[type="hidden"]'),
      text: root.querySelector('[data-listbox-text]'),
      options: () => Array.from(root.querySelectorAll('[role="option"]')),
    };
  }

  function ready(p) {
    return Boolean(p.trigger && p.list);
  }

  function setExpanded(p, open) {
    p.list.hidden = !open;
    p.trigger.setAttribute('aria-expanded', String(open));
    if (!open) {
      p.trigger.removeAttribute('aria-activedescendant');
      for (const item of p.options()) item.classList.remove('is-active');
    }
  }

  function commit(root, value) {
    const p = parts(root);
    const option = p.options().find(item => item.dataset.value === value && item.getAttribute('aria-disabled') !== 'true');
    if (!option) return;
    root.dataset.value = value;
    if (p.input) p.input.value = value;
    if (p.text) p.text.textContent = option.textContent.trim();
    for (const item of p.options()) {
      item.setAttribute('aria-selected', String(item === option));
    }
    const native = root._nativeSelect;
    if (native && native.value !== value) {
      native.value = value;
      native.dispatchEvent(new Event('change', { bubbles: true }));
    }

    const template = root.dataset.listboxNavigate;
    if (template) {
      // Replace only the {value} placeholder, so the page keeps its own query string and
      // the fragment that scopes where the reader lands.
      window.location.assign(template.replace('{value}', encodeURIComponent(value)));
      return;
    }

    if (root.hasAttribute('data-listbox-submit')) {
      const form = root.closest('form');
      if (form) {
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.submit();
      }
    }

    root.dispatchEvent(new CustomEvent('listbox-change', {
      bubbles: true,
      detail: { value: value, label: option.textContent.trim() },
    }));
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

  function openList(root) {
    const p = parts(root);
    if (!ready(p)) return;
    setExpanded(p, true);
    const options = p.options().filter(item => item.getAttribute('aria-disabled') !== 'true');
    const selected = options.find(item => item.dataset.value === currentValue(root));
    highlight(root, selected || options[0]);
  }

  function activeOption(root) {
    const p = parts(root);
    const activeId = p.trigger.getAttribute('aria-activedescendant');
    return p.options().find(item => item.id === activeId) || null;
  }

  function closeAllExcept(root) {
    for (const other of document.querySelectorAll(SELECTOR)) {
      if (other === root) continue;
      const p = parts(other);
      if (ready(p) && !p.list.hidden) setExpanded(p, false);
    }
  }

  document.addEventListener('keydown', event => {
    const target = event.target;
    const root = target && target.closest ? target.closest(SELECTOR) : null;
    if (!root) return;
    const p = parts(root);
    if (!ready(p)) return;

    // Enter and Space belong to the trigger button itself; see the activation model above.
    const onTrigger = target.closest('[data-listbox-trigger]');
    if (onTrigger && (event.key === 'Enter' || event.key === ' ')) return;

    const open = !p.list.hidden;
    const options = p.options().filter(item => item.getAttribute('aria-disabled') !== 'true');
    const activeId = p.trigger.getAttribute('aria-activedescendant');
    const index = Math.max(0, options.findIndex(item => item.id === activeId));
    const selectedIndex = Math.max(0, options.findIndex(item => item.dataset.value === currentValue(root)));

    const move = target => {
      if (!options.length) return;
      const bounded = Math.min(options.length - 1, Math.max(0, target));
      highlight(root, options[bounded]);
    };

    switch (event.key) {
      case 'Enter':
      case ' ':
        event.preventDefault();
        if (!open) openList(root);
        else {
          const chosen = options[index] || options[selectedIndex];
          setExpanded(p, false);
          p.trigger.focus();
          if (chosen) commit(root, chosen.dataset.value);
        }
        return;
      case 'ArrowDown':
        event.preventDefault();
        if (!open) openList(root);
        else move(index + 1);
        return;
      case 'ArrowUp':
        event.preventDefault();
        if (!open) openList(root);
        else move(index - 1);
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
    const target = event.target;
    const root = target && target.closest ? target.closest(SELECTOR) : null;
    closeAllExcept(root);
    if (!root) return;
    const p = parts(root);
    if (!ready(p)) return;

    const option = target.closest('[role="option"]');
    if (option && root.contains(option)) {
      if (option.getAttribute('aria-disabled') === 'true') return;
      setExpanded(p, false);
      p.trigger.focus();
      commit(root, option.dataset.value);
      return;
    }

    if (target.closest('[data-listbox-trigger]')) {
      if (p.list.hidden) {
        openList(root);
        return;
      }
      const active = activeOption(root);
      setExpanded(p, false);
      p.trigger.focus();
      // detail === 0 marks a keyboard-activated click, so Enter commits what the reader
      // highlighted while a mouse click on the trigger only dismisses the list.
      if (event.detail === 0 && active) commit(root, active.dataset.value);
    }
  });

  let listboxSeq = 0;

  function renderSelectOptions(select, root) {
    const p = parts(root);
    if (!p.list) return;
    p.list.replaceChildren();
    const current = select.value;
    root.dataset.value = current;
    const selectedOpt = select.options[select.selectedIndex] || select.options[0];
    if (p.text) p.text.textContent = selectedOpt ? selectedOpt.textContent.trim() : '';
    const baseId = p.list.id || `om-listbox-${++listboxSeq}`;
    Array.from(select.options).forEach((opt, idx) => {
      const li = document.createElement('li');
      li.id = `${baseId}-opt-${idx}`;
      li.setAttribute('role', 'option');
      li.dataset.value = opt.value;
      li.setAttribute('aria-selected', String(opt.value === current));
      if (opt.disabled) {
        li.setAttribute('aria-disabled', 'true');
        li.className = 'is-disabled';
      }
      li.textContent = opt.textContent.trim();
      p.list.appendChild(li);
    });
    if (p.trigger) p.trigger.disabled = Boolean(select.disabled);
  }

  function enhanceSelect(select) {
    if (!(select instanceof HTMLSelectElement)) return;
    if (select.hasAttribute('data-native-select') || select.multiple || select.closest(SELECTOR) || select.closest('[data-library-context]')) return;
    if (select._omakaseListbox) {
      renderSelectOptions(select, select._omakaseListbox);
      return;
    }
    const uid = ++listboxSeq;
    const listId = `om-listbox-list-${uid}`;
    const root = document.createElement('div');
    root.className = 'watch-status-field app-listbox';
    root.setAttribute('data-listbox', '');
    root._nativeSelect = select;
    select._omakaseListbox = root;

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'watch-status-trigger app-listbox-trigger';
    trigger.setAttribute('data-listbox-trigger', '');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', listId);
    const labelSpan = document.createElement('span');
    labelSpan.setAttribute('data-listbox-text', '');
    const labelEl = select.closest('label')?.querySelector('span');
    if (labelEl) {
      labelEl.id ||= `om-listbox-lbl-${uid}`;
      trigger.setAttribute('aria-labelledby', labelEl.id);
    }
    const chev = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chev.setAttribute('viewBox', '0 0 24 24');
    chev.setAttribute('width', '16');
    chev.setAttribute('height', '16');
    chev.setAttribute('fill', 'none');
    chev.setAttribute('stroke', 'currentColor');
    chev.setAttribute('stroke-width', '2');
    chev.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'm6 9 6 6 6-6');
    chev.appendChild(path);
    trigger.append(labelSpan, chev);

    const ul = document.createElement('ul');
    ul.className = 'app-listbox-list';
    ul.id = listId;
    ul.setAttribute('role', 'listbox');
    ul.setAttribute('data-listbox-list', '');
    ul.hidden = true;
    if (labelEl?.id) ul.setAttribute('aria-labelledby', labelEl.id);

    root.append(trigger, ul);
    select.hidden = true;
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;
    select.insertAdjacentElement('afterend', root);
    renderSelectOptions(select, root);
    select.addEventListener('change', () => renderSelectOptions(select, root));
  }

  function enhanceSelects(scope) {
    if (!document.body || !document.body.classList.contains('product-body')) return;
    const root = scope && scope.querySelectorAll ? scope : document;
    root.querySelectorAll('select').forEach(enhanceSelect);
  }

  window.omakaseSyncSelect = enhanceSelect;
  window.omakaseEnhanceSelects = enhanceSelects;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => enhanceSelects(document));
  } else {
    enhanceSelects(document);
  }
  document.addEventListener('omakase:rows-ready', event => enhanceSelects(event.detail));
  document.addEventListener('omakase:sync-selects', event => enhanceSelects(event.detail || document));
})();
