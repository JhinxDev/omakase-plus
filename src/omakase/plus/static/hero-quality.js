/* Home hero only: source pixels decide landscape cover versus a composed portrait. */
(() => {
  "use strict";
  document.querySelectorAll("picture[data-hero-quality]").forEach((picture) => {
    const image = picture.querySelector("img");
    if (!image) return;
    let fallbackAttempted = false;
    let frame = 0;
    let lastSource = "";
    let transparent = false;

    function isTransparent() {
      if (image.naturalWidth <= 3 || image.naturalHeight <= 3) return true;
      try {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 16;
        const context = canvas.getContext("2d", {willReadFrequently: true});
        context.drawImage(image, 0, 0, 16, 16);
        const pixels = context.getImageData(0, 0, 16, 16).data;
        for (let index = 3; index < pixels.length; index += 4) {
          if (pixels[index] > 16) return false;
        }
        return true;
      } catch (_) {
        // A non-readable canvas must not discard otherwise valid artwork.
        return false;
      }
    }

    function fallback() {
      const url = picture.dataset.heroPoster;
      if (fallbackAttempted || !url || new URL(image.currentSrc || image.src, location.href).pathname === new URL(url, location.href).pathname) return;
      fallbackAttempted = true;
      picture.dataset.heroState = "contained";
      picture.querySelectorAll("source").forEach((source) => source.removeAttribute("srcset"));
      image.src = url;
    }

    function assess() {
      if (!image.complete) return;
      // The request can fail before this deferred script attaches its listener.
      if (!image.naturalWidth || !image.naturalHeight) {
        picture.dataset.heroState = "unavailable";
        fallback();
        return;
      }
      const source = image.currentSrc || image.src;
      if (source !== lastSource) {
        lastSource = source;
        transparent = isTransparent();
      }
      const rect = picture.getBoundingClientRect();
      const scale = Math.max(rect.width / image.naturalWidth, rect.height / image.naturalHeight);
      const landscape = image.naturalWidth / image.naturalHeight >= 1.35;
      // Cover tolerates at most 25% CSS-pixel enlargement. Portraits keep
      // their source pixels; a separate blurred layer supplies atmosphere.
      const density = 1;
      picture.style.setProperty("--hero-native-width", `${image.naturalWidth / density}px`);
      picture.style.setProperty("--hero-native-height", `${image.naturalHeight / density}px`);
      picture.dataset.heroState = transparent ? "unavailable" : landscape && scale <= 1.25 ? "cover" : "contained";
      if (transparent || (picture.dataset.heroState === "contained" && !fallbackAttempted)) fallback();
    }

    function schedule() {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(assess);
    }
    image.addEventListener("load", schedule);
    image.addEventListener("error", () => {
      picture.dataset.heroState = "unavailable";
      fallback();
    });
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(schedule).observe(picture);
    window.addEventListener("resize", schedule, {passive: true});
    schedule();
  });
})();
