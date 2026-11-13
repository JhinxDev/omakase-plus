(() => {
  'use strict';
  function updateSubmit(form) {
    const submit = form?.querySelector('[data-credential-submit]');
    if (!submit) return;
    const replacing = [...form.querySelectorAll('[data-credential-editor]')].some(editor => editor.open);
    submit.textContent = replacing ? 'Save replacement key' : submit.dataset.credentialDefault;
  }
  document.querySelectorAll('[data-credential-editor]').forEach(editor => {
    const input = editor.querySelector('[data-credential-input]');
    const form = editor.closest('form');
    const sync = () => {
      input.disabled = !editor.open;
      input.required = editor.open;
      if (!editor.open) input.value = '';
      updateSubmit(form);
    };
    editor.addEventListener('toggle', sync);
    editor.querySelector('[data-credential-cancel]').addEventListener('click', () => {
      input.value = '';
      editor.open = false;
      sync();
      editor.querySelector('summary').focus();
    });
    sync();
  });
})();
