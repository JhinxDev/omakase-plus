/* A local preview only. The server independently validates and re-encodes uploads. */
(() => {
  const form = document.querySelector('[data-avatar-form]');
  if (!form || !window.createImageBitmap) return;
  const input = form.querySelector('input[type="file"]');
  const preview = document.querySelector('[data-avatar-preview]');
  const current = document.querySelector('[data-avatar-current]');
  const status = form.querySelector('[data-avatar-status]');
  let revision = 0;
  const restore = () => { preview.hidden = true; current.hidden = false; };
  input.addEventListener('change', async () => {
    const own = ++revision;
    restore();
    status.hidden = true;
    input.setCustomValidity('');
    const file = input.files[0];
    if (!file) return;
    const fail = message => {
      if (own !== revision) return;
      input.setCustomValidity(message);
      status.textContent = message;
      status.hidden = false;
    };
    if (file.size > 8 * 1024 * 1024) return fail('Choose a picture no larger than 8 MiB.');
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) return fail('Choose a JPEG, PNG or WebP picture.');
    let bitmap;
    try {
      bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
      if (own !== revision) return;
      if (bitmap.width > 8192 || bitmap.height > 8192 || bitmap.width * bitmap.height > 16000000) return fail('Choose a picture with at most 16 million pixels.');
      const edge = Math.min(bitmap.width, bitmap.height);
      const context = preview.getContext('2d');
      context.clearRect(0, 0, 192, 192);
      context.drawImage(bitmap, (bitmap.width - edge) / 2, (bitmap.height - edge) / 2, edge, edge, 0, 0, 192, 192);
      preview.hidden = false;
      current.hidden = true;
      status.textContent = 'Preview only. Upload the picture to save this change.';
      status.hidden = false;
    } catch (_) {
      fail('This picture could not be previewed. Choose another JPEG, PNG or WebP.');
    } finally {
      if (bitmap) bitmap.close();
    }
  });
})();
