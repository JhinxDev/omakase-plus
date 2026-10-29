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

    function sampleArtwork() {
      if (image.naturalWidth <= 3 || image.naturalHeight <= 3) return {transparent: true};
      try {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 16;
        const context = canvas.getContext("2d", {willReadFrequently: true});
        context.drawImage(image, 0, 0, 16, 16);
        const pixels = context.getImageData(0, 0, 16, 16).data;
        const colors = new Map();
        const total = [0, 0, 0];
        let opaque = 0;
        for (let index = 0; index < pixels.length; index += 4) {
          if (pixels[index + 3] <= 16) continue;
          const rgb = [pixels[index], pixels[index + 1], pixels[index + 2]];
          opaque += 1;
          rgb.forEach((value, channel) => { total[channel] += value; });
          const high = Math.max(...rgb), low = Math.min(...rgb);
          const saturation = high ? (high - low) / high : 0;
          const light = rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
          if (light < 28 || light > 225 || saturation < .12) continue;
          const key = rgb.map(value => Math.floor(value / 32)).join(',');
          const color = colors.get(key) || {score: 0, count: 0, rgb: [0, 0, 0]};
          color.score += .25 + saturation;
          color.count += 1;
          rgb.forEach((value, channel) => { color.rgb[channel] += value; });
          colors.set(key, color);
        }
        if (!opaque) return {transparent: true};
        const ranked = [...colors.values()].sort((a, b) => b.score - a.score)
          .map(color => color.rgb.map(value => Math.round(value / color.count)));
        const primary = ranked[0] || total.map(value => Math.round(value / opaque));
        const secondary = ranked.find(rgb => Math.hypot(...rgb.map((value, channel) => value - primary[channel])) > 70) || primary;
        return {transparent: false, primary, secondary};
      } catch (_) {
        // A non-readable canvas must not discard otherwise valid artwork.
        return {transparent: false};
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
        const sampled = sampleArtwork();
        transparent = sampled.transparent;
        const hero = picture.closest('.cinema-hero');
        if (hero && sampled.primary) {
          hero.style.setProperty('--hero-color-primary', sampled.primary.join(','));
          hero.style.setProperty('--hero-color-secondary', sampled.secondary.join(','));
        }
      }
      const rect = picture.getBoundingClientRect();
      const scale = Math.max(rect.width / image.naturalWidth, rect.height / image.naturalHeight);
      const landscape = image.naturalWidth / image.naturalHeight >= 1.35;
      // Cover tolerates at most 25% CSS-pixel enlargement. Portraits keep
      // their source pixels and exact aspect; sampled gradients supply atmosphere.
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
