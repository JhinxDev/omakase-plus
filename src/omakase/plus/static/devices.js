(() => {
  "use strict";
  const copy = document.querySelector("[data-copy-device-link]");
  const link = copy && document.getElementById(copy.dataset.copyDeviceLink);
  const status = document.querySelector("[data-device-copy-status]");
  if (!copy || !link || !status) return;
  copy.hidden = false;
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(link.value);
      status.textContent = "Link copied.";
    } catch {
      link.focus();
      link.select();
      status.textContent = "Select and copy the link.";
    }
  });
})();
