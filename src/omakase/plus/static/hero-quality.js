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
    const regions = {
      tl: [0, 0, 6, 6], top: [24, 0, 16, 4], tr: [58, 0, 6, 6],
      left: [0, 24, 4, 16], right: [60, 24, 4, 16],
      bl: [0, 58, 6, 6], bottom: [24, 60, 16, 4], br: [58, 58, 6, 6],
    };

    function clearPalette() {
      const hero = picture.closest('.cinema-hero');
      if (!hero) return;
      Object.keys(regions).forEach(region => hero.style.removeProperty(`--hero-edge-${region}`));
      hero.dataset.heroPalette = 'fallback';
    }

    function sampleArtwork() {
      if (image.naturalWidth <= 3 || image.naturalHeight <= 3) return {transparent: true};
      try {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 64;
        const context = canvas.getContext("2d", {willReadFrequently: true});
        context.drawImage(image, 0, 0, 64, 64);
        const pixels = context.getImageData(0, 0, 64, 64).data;
        if (!pixels.some((value, index) => index % 4 === 3 && value > 16)) return {transparent: true};
        const edges = {};
        for (const [region, [x, y, width, height]] of Object.entries(regions)) {
          const total = [0, 0, 0], chromatic = [0, 0, 0];
          let opaque = 0, chromaticWeight = 0;
          for (let row = y; row < y + height; row += 1) {
            for (let column = x; column < x + width; column += 1) {
              const index = (row * 64 + column) * 4;
              if (pixels[index + 3] <= 16) continue;
              const rgb = [pixels[index], pixels[index + 1], pixels[index + 2]];
              const weight = pixels[index + 3] / 255;
              opaque += weight;
              rgb.forEach((value, channel) => { total[channel] += value * weight; });
              const high = Math.max(...rgb), low = Math.min(...rgb);
              const saturation = high ? (high - low) / high : 0;
              const light = rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
              if (light < 20 || light > 232 || saturation < .1) continue;
              const colorWeight = weight * (.5 + saturation);
              chromaticWeight += colorWeight;
              rgb.forEach((value, channel) => { chromatic[channel] += value * colorWeight; });
            }
          }
          edges[region] = chromaticWeight ? chromatic.map(value => Math.round(value / chromaticWeight))
            : opaque ? total.map(value => Math.round(value / opaque)) : [22, 25, 35];
        }
        return {transparent: false, edges};
      } catch (_) {
        // A non-readable canvas must not discard otherwise valid artwork.
        return {transparent: false};
      }
    }

    function placeAtmosphere() {
      const hero = picture.closest('.cinema-hero');
      if (!hero) return;
      const bounds = hero.getBoundingClientRect(), art = image.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      const anchors = {xleft: [art.left - bounds.left, bounds.width], xcenter: [art.left + art.width / 2 - bounds.left, bounds.width], xright: [art.right - bounds.left, bounds.width], ytop: [art.top - bounds.top, bounds.height], ycenter: [art.top + art.height / 2 - bounds.top, bounds.height], ybottom: [art.bottom - bounds.top, bounds.height]};
      Object.entries(anchors).forEach(([name, [value, total]]) => hero.style.setProperty(`--hero-edge-${name}`, `${Math.max(0, Math.min(100, value / total * 100)).toFixed(2)}%`));
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
        clearPalette();
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
        clearPalette();
        if (hero && sampled.edges) {
          Object.entries(sampled.edges).forEach(([region, rgb]) => hero.style.setProperty(`--hero-edge-${region}`, rgb.join(',')));
          hero.dataset.heroPalette = 'perimeter-v1';
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
      placeAtmosphere();
      if (transparent || (picture.dataset.heroState === "contained" && !fallbackAttempted)) fallback();
    }

    function schedule() {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(assess);
    }
    image.addEventListener("load", () => { lastSource = ''; schedule(); });
    image.addEventListener("error", () => {
      clearPalette();
      picture.dataset.heroState = "unavailable";
      fallback();
    });
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(schedule).observe(picture);
    window.addEventListener("resize", schedule, {passive: true});
    schedule();
  });
})();
