(() => {
  let loadingTimer = null;
  let loadingStart = 0;
  let navigationTimer = null;
  const busyButtons = new Map();

  const byId = (id) => document.getElementById(id);
  const csrfToken = () =>
    document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
  const jsonHeaders = () => ({
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken(),
  });

  function intOrNull(value) {
    const parsed = parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function tickLoading() {
    const elapsed = Math.floor((Date.now() - loadingStart) / 1000);
    const elapsedNode = byId("loading-elapsed");
    const textNode = byId("loading-text");
    if (elapsedNode) elapsedNode.textContent = `${elapsed}s`;
    if (!textNode) return;

    const phases = [
      { at: 0, text: "Finding titles that match your taste…" },
      { at: 15, text: "Your request is working. The shortlist will appear here when ready." },
      { at: 45, text: "Your AI is considering the shortlist. This can take a few minutes." },
      { at: 90, text: "Still working. You can leave this page and return to the same run." },
    ];
    let phase = phases[0];
    for (const nextPhase of phases) {
      if (elapsed >= nextPhase.at) phase = nextPhase;
    }
    textNode.textContent = phase.text;
  }

  function renderResults(data) {
    const container = byId("results-list");
    if (!container || !data || !Array.isArray(data.recommendations)) return;
    container.replaceChildren();
    for (const rec of data.recommendations) {
      const score = Number(rec.predicted_score);
      let scoreClass = "score-mid";
      if (score >= 8) scoreClass = "score-high";
      else if (score < 6) scoreClass = "score-low";

      const card = document.createElement("article");
      card.className = "rec-card";
      const top = document.createElement("div");
      top.className = "rec-card-top";
      const title = document.createElement("h3");
      title.className = "rec-title";
      title.textContent = rec.title || "Untitled recommendation";
      const scoreNode = document.createElement("span");
      scoreNode.className = `score ${scoreClass}`;
      scoreNode.textContent = `${rec.predicted_score}/10`;
      top.append(title, scoreNode);
      card.append(top);
      const reasoning = document.createElement("p");
      reasoning.className = "reasoning";
      reasoning.textContent = rec.reasoning || "";
      card.append(reasoning);
      if (rec.best_match_from_history) {
        const match = document.createElement("p");
        match.className = "match";
        match.append("Pairs with: ");
        const strong = document.createElement("strong");
        strong.textContent = rec.best_match_from_history;
        match.append(strong);
        card.append(match);
      }
      container.append(card);
    }
  }

  function setLoading(active) {
    const button = byId("run-btn");
    const loading = byId("loading");
    if (button) {
      button.disabled = active;
      button.setAttribute("aria-busy", active ? "true" : "false");
    }
    if (loading) loading.hidden = !active;
    if (active) {
      loadingStart = Date.now();
      loadingTimer = window.setInterval(tickLoading, 1000);
      tickLoading();
    } else if (loadingTimer) {
      window.clearInterval(loadingTimer);
      loadingTimer = null;
    }
  }

  function showRunError(message) {
    const error = byId("run-error");
    setLoading(false);
    if (!error) return;
    error.textContent = message;
    error.hidden = false;
    error.tabIndex = -1;
    error.focus();
  }

  async function runRecs(event) {
    event.preventDefault();
    const error = byId("run-error");
    if (error) error.hidden = true;
    setLoading(true);

    const selectedLane = document.querySelector('input[name="lane"]:checked');
    const lane = selectedLane ? selectedLane.value : "best_match";
    const body = JSON.stringify({
      source: byId("run-source").value,
      username: byId("run-username").value,
      mode: byId("run-mode").value,
      count: parseInt(byId("run-count").value, 10),
      temperature: parseFloat(byId("run-temp").value),
      provider: byId("run-provider")?.value || "configured",
      model: byId("run-provider") ? (byId("run-provider").value === "codex" ? byId("run-model").value : "") : byId("run-model").value,
      lane,
      use_planning: lane === "plan_list" || byId("run-planning").checked,
      skip_profile: byId("run-skip").checked,
      max_episodes: parseInt(byId("run-max-episodes")?.value || "0", 10),
      completed_only: byId("run-completed")?.checked || false,
      mood: byId("run-mood")?.value || "",
    });

    let job;
    try {
      const response = await fetch("/plus/api/run", {
        method: "POST",
        headers: jsonHeaders(),
        body,
      });
      job = await response.json();
    } catch (errorStart) {
      showRunError(`Network error starting run: ${errorStart.message}`);
      return;
    }

    if (!job || job.status !== "running" || !job.job_id) {
      showRunError(`Could not start run: ${(job && job.detail) || "unexpected response"}`);
      return;
    }

    pollRun(job);
  }

  function pollRun(job) {
    const deadline = Date.now() + 10 * 60 * 1000;
    const poll = async () => {
      if (Date.now() > deadline) {
        showRunError("Run is taking unusually long. Refresh the page to check for results.");
        return;
      }

      let data;
      try {
        const response = await fetch(`/plus/api/run/status/${encodeURIComponent(job.job_id)}`);
        data = await response.json();
      } catch {
        window.setTimeout(poll, 3000);
        return;
      }

      if (data.status === "running") {
        window.setTimeout(poll, 3000);
        return;
      }

      if (data.status === "ok") {
        renderResults(data);
        const successUrl = document.getElementById("run-form")?.dataset.successUrl;
        const runId = intOrNull(data.run_id);
        const destination =
          successUrl === "/plus/discover"
            ? `${successUrl}?run=${encodeURIComponent(runId || "")}`
            : `/plus/dashboard?run=${encodeURIComponent(runId || "")}`;
        window.location.assign(destination);
        return;
      }

      showRunError(`Recommendation failed: ${data.detail || "unknown error"}`);
    };

    poll();
  }

  function setupRunForm() {
    const form = byId("run-form");
    if (form) {
      form.addEventListener("submit", runRecs);
      if (form.dataset.activeJob) {
        setLoading(true);
        pollRun({ job_id: form.dataset.activeJob });
      }
    }
    const provider = byId("run-provider");
    if (provider) {
      const updateProvider = () => {
        const codex = provider.value === "codex";
        byId("codex-options").hidden = !codex;
        byId("configured-options").hidden = codex;
      };
      provider.addEventListener("change", updateProvider);
      updateProvider();
    }
  }

  function setupFeedback() {
    document.querySelectorAll(".btn-feedback").forEach((button) => {
      button.addEventListener("click", async () => {
        const group = button.closest(".feedback-actions");
        if (!group) return;
        const original = button.textContent;
        const scoreNode = group.querySelector("[data-feedback-score]");
        const score =
          button.dataset.feedback === "already_watched" && scoreNode
            ? scoreNode.value || null
            : null;
        button.disabled = true;
        setButtonBusy(button, "Saving…");
        try {
          const response = await fetch("/plus/api/feedback", {
            method: "POST",
            headers: jsonHeaders(),
            body: JSON.stringify({
              feedback_type: button.dataset.feedback,
              title: group.dataset.title,
              source: group.dataset.source || "anilist",
              media_id: intOrNull(group.dataset.mediaId),
              run_id: intOrNull(group.dataset.runId),
              score,
            }),
          });
          const data = await response.json();
          if (!response.ok || !data || data.status !== "ok") throw new Error();
          const synced = data.tracker_status === "synced";
          clearButtonBusy(button);
          button.textContent = synced ? "Added to AniList" : "Saved";
          group.dataset.saved = button.dataset.feedback;
          const confirmation =
            data.detail ||
            (synced
              ? "Marked watched on your AniList account."
              : "This signal will shape future recommendations.");
          const resultArea = byId("results-list");
          if (resultArea) {
            resultArea.className = "product-notice";
            resultArea.setAttribute("role", "status");
            resultArea.textContent = confirmation;
          }
          group.closest("article")?.remove();
          if (!resultArea) showActionStatus(group, confirmation, "success");
        } catch {
          clearButtonBusy(button);
          button.disabled = false;
          button.textContent = original;
          showActionStatus(group, "Could not save this action. Try again.", "error");
        }
      });
    });
  }

  function showActionStatus(group, message, state) {
    let status = group.querySelector(".feedback-status");
    if (!status) {
      status = document.createElement("p");
      status.className = "feedback-status";
      status.setAttribute("role", "status");
      group.append(status);
    }
    status.dataset.state = state;
    status.textContent = message;
  }

  function setButtonBusy(button, label) {
    if (!busyButtons.has(button)) busyButtons.set(button, button.textContent);
    button.setAttribute("aria-busy", "true");
    button.classList.add("is-busy");
    button.textContent = label;
  }

  function clearButtonBusy(button, restore = false) {
    button.removeAttribute("aria-busy");
    button.classList.remove("is-busy");
    if (restore && busyButtons.has(button)) button.textContent = busyButtons.get(button);
    busyButtons.delete(button);
  }

  function showNavigationLoading(label = "Loading…") {
    if (navigationTimer !== null) window.clearTimeout(navigationTimer);
    navigationTimer = window.setTimeout(() => {
      const loading = byId("navigation-loading");
      if (!loading) return;
      loading.querySelector(".navigation-loading-label").textContent = label;
      loading.hidden = false;
    }, 140);
  }

  function clearNavigationLoading() {
    window.clearTimeout(navigationTimer);
    navigationTimer = null;
    const loading = byId("navigation-loading");
    if (loading) loading.hidden = true;
  }

  function setupNavigationLoading() {
    document.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target.closest("a[href]");
      if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
      const url = new URL(link.href, location.href);
      if (url.origin !== location.origin || (url.pathname === location.pathname && url.search === location.search)) return;
      showNavigationLoading();
    });
    document.addEventListener("submit", (event) => {
      const form = event.target;
      if (event.defaultPrevented || form.id === "run-form" || (form.target && form.target !== "_self")) return;
      if (new URL(form.action, location.href).origin !== location.origin) return;
      if (form.dataset.pendingSubmit) { event.preventDefault(); return; }
      form.dataset.pendingSubmit = "true";
      const button = event.submitter;
      const label = button?.dataset.busyLabel || (form.method.toLowerCase() === "get" ? "Loading…" : "Saving…");
      if (button) {
        // Keep the native submitter enabled so its name/value is still submitted.
        button.setAttribute("aria-disabled", "true");
        setButtonBusy(button, label);
      }
      showNavigationLoading(label);
    });
    window.addEventListener("pagehide", clearNavigationLoading);
    window.addEventListener("pageshow", () => {
      clearNavigationLoading();
      document.querySelectorAll("form[data-pending-submit]").forEach(form => { delete form.dataset.pendingSubmit; });
      for (const button of Array.from(busyButtons.keys())) {
        button.removeAttribute("aria-disabled");
        button.disabled = false;
        clearButtonBusy(button, true);
      }
    });
  }

  function setupPlanButtons() {
    document.querySelectorAll(".btn-plan").forEach((button) => {
      button.addEventListener("click", async () => {
        const original = button.textContent;
        const group = button.closest(".feedback-actions, .pick-actions");
        button.disabled = true;
        setButtonBusy(button, "Saving…");
        try {
          const response = await fetch("/plus/api/plan", {
            method: "POST",
            headers: jsonHeaders(),
            body: JSON.stringify({
              anilist_id: intOrNull(button.dataset.mediaId),
              title: button.dataset.title,
              status: "PLANNING",
            }),
          });
          const data = await response.json();
          if (
            !response.ok ||
            !data ||
            !["ok", "already_planned"].includes(data.status)
          ) {
            throw new Error(data?.detail || "Could not add to plan");
          }
          clearButtonBusy(button);
          button.textContent = data.status === "already_planned" ? "Already saved" : "Saved to watchlist";
          if (group) showActionStatus(group, data.detail || "Saved for later in your watchlist.", "success");
        } catch (error) {
          clearButtonBusy(button);
          button.disabled = false;
          button.textContent = original;
          if (group) showActionStatus(group, error.message, "error");
        }
      });
    });
  }

  function setupBusyForms() {
    document.querySelectorAll("[data-busy-form]").forEach((form) => {
      form.addEventListener("submit", (event) => {
        if (event.defaultPrevented) return;
        const button = form.querySelector("button[type='submit']");
        if (!button) return;
        const busyLabel = button.dataset.busyLabel;
        if (busyLabel) setButtonBusy(button, busyLabel);
      });
    });
  }

  function setupPosterFallbacks() {
    document.querySelectorAll("[data-poster]").forEach((image) => {
      image.addEventListener("error", () => {
        image.hidden = true;
        image.closest(".poster-frame")?.classList.add("poster-unavailable");
      });
    });
  }

  function setupAccountMenu() {
    const menu = document.querySelector("[data-account-menu]");
    if (!menu) return;
    document.addEventListener("click", (event) => {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      menu.open = false;
      menu.querySelector("summary")?.focus();
    });
  }

  function setupCompletedSelection() {
    const form = document.querySelector(".completed-cleanup-selection");
    if (!form) return;
    const choices = Array.from(form.querySelectorAll('input[name="monitor_id"]'));
    const button = form.querySelector('button[type="submit"]');
    if (!button || choices.length === 0) return;
    const idleLabel = button.textContent;
    const update = () => {
      const count = choices.filter((choice) => choice.checked).length;
      button.disabled = count === 0;
      button.textContent = count
        ? `Clean up ${count} selected title${count === 1 ? "" : "s"}`
        : idleLabel;
    };
    choices.forEach((choice) => choice.addEventListener("change", update));
    update();
  }

  function setupBulkChoices() {
    const status = byId("bulk-selection-status");
    const choices = Array.from(document.querySelectorAll(".choice-option input[type='radio']"));
    if (!status || choices.length === 0) return;
    const update = () => {
      const groups = new Set(choices.map((choice) => choice.name));
      const resolved = new Set(choices.filter((choice) => choice.checked).map((choice) => choice.name));
      status.textContent =
        resolved.size === groups.size
          ? "Every uncertain request now has an exact title. Ready to confirm."
          : `${groups.size - resolved.size} ambiguous request${groups.size - resolved.size === 1 ? "" : "s"} still need a choice.`;
    };
    choices.forEach((choice) => choice.addEventListener("change", update));
    update();
  }

  function setupLocalDateTimes() {
    const formatter = new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
      timeZoneName: "short",
    });
    document.querySelectorAll("[data-local-datetime]").forEach((node) => {
      const raw = node.dataset.localDatetime?.trim();
      if (!raw) return;
      const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
      const isoValue = hasTimezone ? raw : `${raw.replace(" ", "T")}Z`;
      const date = new Date(isoValue);
      if (Number.isNaN(date.getTime())) return;
      node.dateTime = date.toISOString();
      node.textContent = formatter.format(date);
      node.title = `${date.toISOString().replace(".000Z", "Z")} · shown in your local timezone`;
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll('[data-open-details]').forEach((link) => {
      link.addEventListener('click', () => {
        const panel = document.getElementById(link.hash.slice(1));
        if (panel instanceof HTMLDetailsElement) {
          panel.open = true;
          panel.querySelector('summary')?.focus({ preventScroll: true });
        }
      });
    });
    if (location.hash === '#watch-tracking') {
      const panel = document.getElementById('watch-tracking');
      if (panel) panel.open = true;
    }
    setupRunForm();
    setupFeedback();
    setupPlanButtons();
    setupBusyForms();
    setupNavigationLoading();
    setupPosterFallbacks();
    setupAccountMenu();
    setupCompletedSelection();
    setupBulkChoices();
    setupLocalDateTimes();
  });

  window.runRecs = runRecs;
})();
