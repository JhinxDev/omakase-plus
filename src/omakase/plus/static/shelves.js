/* Native scrolling remains the fallback; controls enhance overflowing shelves. */
(() => {
  'use strict';
  document.querySelectorAll('[data-mobile-browse]').forEach(menu => {
    document.addEventListener('click', event => { if (menu.open && !menu.contains(event.target)) menu.open = false; });
    menu.addEventListener('keydown', event => { if (event.key === 'Escape') { menu.open = false; menu.querySelector('summary').focus(); } });
  });
  document.querySelectorAll('.settings-navigation').forEach(nav => {
    nav.addEventListener('focusin', event => {
      const link = event.target.closest('a');
      if (link && nav.scrollWidth > nav.clientWidth) {
        link.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'auto'});
      }
    });
    const active = nav.querySelector('[aria-current="page"]');
    if (active && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft = Math.max(0, active.offsetLeft - nav.offsetLeft - (nav.clientWidth - active.clientWidth) / 2);
    }
  });
  document.querySelectorAll('.media-rail').forEach((rail, index) => {
    const shelf = document.createElement('div');
    shelf.className = 'media-shelf';
    rail.before(shelf);
    shelf.append(rail);
    rail.id ||= `media-shelf-${index + 1}`;
    const title = rail.getAttribute('aria-label') || 'titles';
    const move = direction => {
      const cards = [...rail.children];
      if (!cards.length) return;
      const bounds = rail.getBoundingClientRect();
      const end = Math.max(0, rail.scrollWidth - rail.clientWidth);
      const points = [...new Set(cards.map(card => Math.min(end,
        Math.max(0, card.getBoundingClientRect().left - bounds.left + rail.scrollLeft))))];
      const current = rail.scrollLeft;
      const candidates = direction > 0 ? points.filter(p => p > current + 2) : points.filter(p => p < current - 2).reverse();
      const page = Math.max(rail.clientWidth * .8, 160);
      const target = candidates.find(p => Math.abs(p - current) >= page) ?? candidates[candidates.length - 1] ?? (direction > 0 ? end : 0);
      rail.scrollTo({left: target, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    };
    const controls = [-1, 1].map(direction => {
      const button = document.createElement('button');
      button.className = `shelf-arrow shelf-arrow-${direction < 0 ? 'previous' : 'next'}`;
      button.type = 'button';
      button.setAttribute('aria-label', `${direction < 0 ? 'Previous' : 'Next'} ${title}`);
      button.setAttribute('aria-controls', rail.id);
      const icon = document.createElement('span');
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = direction < 0 ? '‹' : '›';
      button.append(icon);
      button.addEventListener('click', () => move(direction));
      shelf.append(button);
      return button;
    });
    const update = () => {
      const end = rail.scrollWidth - rail.clientWidth;
      shelf.classList.toggle('has-overflow', end > 2);
      controls[0].disabled = rail.scrollLeft <= 2;
      controls[1].disabled = rail.scrollLeft >= end - 2;
    };
    rail.addEventListener('scroll', update, {passive: true});
    new ResizeObserver(update).observe(rail);
    rail.classList.add('shelf-enhanced');
    update();
  });
})();
