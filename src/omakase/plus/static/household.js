(() => {
  'use strict';
  document.querySelectorAll('[data-household-expiry]').forEach(node => {
    const seconds = Number(node.dataset.householdExpiry);
    if (Number.isFinite(seconds)) node.textContent = new Date(seconds * 1000).toLocaleString();
  });
  document.querySelectorAll('[data-household-poster]').forEach(image => {
    const unavailable = () => { image.hidden = true; };
    image.addEventListener('error', unavailable, { once: true });
    if (image.complete && !image.naturalWidth) unavailable();
  });
  const copy = document.querySelector('[data-copy-household-code]');
  copy?.addEventListener('click', async () => {
    const code = document.querySelector('#created-invite-code');
    const status = document.querySelector('[data-household-copy-status]');
    try { await navigator.clipboard.writeText(code.value); status.textContent = 'Invitation code copied.'; }
    catch { code.focus(); code.select(); status.textContent = 'Select and copy this invitation code.'; }
  });
})();
