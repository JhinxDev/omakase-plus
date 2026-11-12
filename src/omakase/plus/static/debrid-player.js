(() => {
  const video = document.querySelector('video[data-cloud-playlist]');
  const status = document.querySelector('[data-cloud-status]');
  if (!video || !status) return;

  const progressUrl = video.dataset.cloudProgressUrl || '';
  const csrf = video.dataset.requestValue || '';
  const fileKey = video.dataset.cloudFile || '';
  const title = video.dataset.cloudTitle || '';
  const saved = Number(video.dataset.cloudPosition || '0') || 0;
  let lastSent = null;

  const showError = () => {
    status.hidden = false;
    status.textContent = 'Could not open this stream. Return to Cloud files and try again.';
  };

  const clock = (value) => {
    const total = Math.max(0, Math.floor(value));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
      : `${minutes}:${String(seconds).padStart(2, '0')}`;
  };

  const send = (force) => {
    if (!progressUrl || !fileKey || !Number.isFinite(video.duration) || video.duration < 10) return;
    const position = video.currentTime;
    const value = video.duration - position < 30 ? 0 : position;
    if (!force && lastSent !== null && Math.abs(value - lastSent) < 3) return;
    lastSent = value;
    const body = new URLSearchParams({
      csrf_token: csrf,
      file_key: fileKey,
      title,
      position: String(value),
      duration: String(video.duration),
    });
    fetch(progressUrl, {
      method: 'POST',
      body,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      keepalive: force,
    }).catch(() => {});
  };

  video.addEventListener('error', showError);
  video.addEventListener('loadedmetadata', () => {
    status.hidden = true;
    if (saved > 5 && saved < video.duration - 5) {
      try {
        video.currentTime = saved;
        status.hidden = false;
        status.textContent = `Resuming at ${clock(saved)}…`;
      } catch {
        status.hidden = true;
      }
    }
  }, { once: true });
  video.addEventListener('playing', () => { status.hidden = true; });
  video.addEventListener('pause', () => send(true));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') send(true);
  });
  window.addEventListener('pagehide', () => send(true));
  setInterval(() => {
    if (!video.paused && !video.ended) send(false);
  }, 5000);

  async function load() {
    const source = video.dataset.cloudPlaylist;
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = source;
      return;
    }
    const shaka = window.shaka;
    if (!shaka?.Player.isBrowserSupported()) {
      showError();
      return;
    }
    const player = new shaka.Player();
    player.addEventListener('error', showError);
    window.addEventListener('pagehide', () => { void player.destroy(); }, { once: true });
    await player.attach(video);
    await player.load(source);
  }
  load().catch(showError);
})();
