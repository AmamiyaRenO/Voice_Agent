(function () {
  "use strict";

  const cockpitPages = new Set(["dashboard", "controls"]);
  const state = {
    healthTimer: 0,
    cameraTimer: 0,
    cameraActive: false,
    latestHealth: null,
    latestAsr: null,
    logEntries: [],
    logVersion: -1,
    logTab: "conversation",
    logSource: null,
    logTimer: 0
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }

  function icons(root) {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons({ attrs: { "aria-hidden": "true" }, root: root || document });
    }
  }

  async function api(url, options) {
    const init = Object.assign({ cache: "no-store" }, options || {});
    const headers = new Headers(init.headers || {});
    if (init.body && typeof init.body !== "string" && !(init.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(init.body);
    }
    init.headers = headers;
    const response = await fetch(url, init);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      const detail = payload && typeof payload === "object" ? payload.detail || payload.message : payload;
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return payload;
  }

  function toast(message, kind) {
    const region = document.getElementById("toast-region");
    if (!region) return;
    const item = document.createElement("div");
    item.className = `toast ${kind || "info"}`;
    item.textContent = String(message || "Done");
    region.appendChild(item);
    window.setTimeout(() => item.remove(), 5200);
  }

  function confirmAction(options) {
    const settings = Object.assign({ title: "Confirm action", message: "Continue?", confirmLabel: "Confirm", danger: false }, options || {});
    return new Promise((resolve) => {
      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      backdrop.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <h2 id="confirm-title">${escapeHtml(settings.title)}</h2>
        <p class="panel-note">${escapeHtml(settings.message)}</p>
        <div class="modal-actions"><button class="secondary" data-cancel type="button">Cancel</button><button class="${settings.danger ? "danger" : "primary"}" data-confirm type="button">${escapeHtml(settings.confirmLabel)}</button></div>
      </div>`;
      document.body.appendChild(backdrop);
      const finish = (value) => { backdrop.remove(); resolve(value); };
      backdrop.querySelector("[data-cancel]").addEventListener("click", () => finish(false));
      backdrop.querySelector("[data-confirm]").addEventListener("click", () => finish(true));
      backdrop.addEventListener("click", (event) => { if (event.target === backdrop) finish(false); });
      backdrop.querySelector("[data-cancel]").focus();
    });
  }

  function setBusy(element, busy, busyText) {
    if (!element) return;
    if (busy) {
      element.dataset.previousHtml = element.innerHTML;
      if (busyText) element.textContent = busyText;
      element.disabled = true;
      element.setAttribute("aria-busy", "true");
    } else {
      if (element.dataset.previousHtml != null) element.innerHTML = element.dataset.previousHtml;
      delete element.dataset.previousHtml;
      element.disabled = false;
      element.removeAttribute("aria-busy");
      icons(element);
    }
  }

  function topbarMarkup(isCockpit) {
    return `<header class="app-topbar">
      <a class="topbar-brand" href="/index.html" aria-label="Rachel Console home"><span class="topbar-mark">R</span><span>Rachel Console</span></a>
      <div class="topbar-actions">
        ${isCockpit ? '<button class="icon-button cockpit-toggle" id="toggle-cockpit" type="button" aria-label="Collapse device cockpit" title="Collapse device cockpit"><i data-lucide="panel-top-close"></i></button>' : ""}
        ${isCockpit ? "" : '<a class="icon-button" href="/index.html" aria-label="Home" title="Home"><i data-lucide="house"></i></a>'}
        <a class="icon-button" href="/runtime.html" aria-label="Settings" title="Settings"><i data-lucide="settings"></i></a>
      </div>
    </header>`;
  }

  function cockpitMarkup() {
    return `<aside class="device-cockpit" aria-label="Rachel device cockpit">
      <div class="device-stage">
        <div class="stage-floor"></div>
        <div class="device-visual">
          <img class="device-image" src="/panel-assets/rachel-device.png" alt="Rachel assistive robot">
          <img class="device-face-preview" id="device-face-preview" src="/panel-assets/face-neutral.png" alt="Rachel neutral expression">
        </div>
        <button class="icon-button device-power" id="device-power" type="button" aria-label="Start listening" title="Start or pause listening"><i data-lucide="power"></i></button>
        <a class="icon-button device-settings" href="/runtime.html" aria-label="Open settings" title="Settings"><i data-lucide="settings"></i></a>
        <span class="device-state checking" id="device-state">Checking</span>
        <button class="camera-peek" id="camera-toggle" type="button" aria-label="Turn camera preview on">
          <img id="camera-preview" alt="Rachel camera preview" hidden>
          <span class="camera-placeholder" id="camera-placeholder"><i data-lucide="camera-off"></i><span>Camera Off</span></span>
        </button>
      </div>
      <div class="device-heading">
        <div><div class="device-name">Rachel</div><div class="device-version" id="runtime-label">Runtime checking</div></div>
        <span class="status-badge" id="provider-badge">Provider</span>
      </div>
      <div class="audio-summary">
        <div class="audio-card"><div class="audio-label"><span>Speaker</span><i data-lucide="volume-2"></i></div><div class="audio-value" id="voice-value">Idle</div><div class="mini-meter"><span id="voice-meter"></span></div></div>
        <div class="audio-card"><div class="audio-label"><span>Microphone</span><i data-lucide="mic"></i></div><div class="audio-value" id="microphone-value">Waiting for status</div><div class="mini-meter"><span id="microphone-meter"></span></div></div>
      </div>
      <section class="cockpit-logs" aria-label="Session log">
        <div class="cockpit-log-head"><div class="cockpit-log-tabs" role="tablist"><button class="active" type="button" data-cockpit-log="conversation" aria-selected="true">Conversation</button><button type="button" data-cockpit-log="events" aria-selected="false">Events</button></div><button class="ghost" id="refresh-cockpit-log" type="button" aria-label="Refresh logs" title="Refresh logs"><i data-lucide="refresh-cw"></i></button></div>
        <div class="compact-log" id="cockpit-log" role="log" aria-live="polite"><div class="muted">Waiting for session activity.</div></div>
      </section>
    </aside>`;
  }

  function mountShell() {
    const body = document.body;
    const page = body.dataset.page || "dashboard";
    const isCockpit = cockpitPages.has(page);
    const content = body.querySelector("[data-page-content]");
    if (!content) throw new Error("Page is missing [data-page-content]");
    body.classList.add("console-page", isCockpit ? "shell-cockpit" : "shell-utility");
    const shell = document.createElement("div");
    shell.className = "app-shell";
    shell.innerHTML = `${topbarMarkup(isCockpit)}${isCockpit ? cockpitMarkup() : ""}<main class="app-main"><div class="content-width" id="page-root"></div></main><div class="toast-region" id="toast-region" aria-live="polite"></div>`;
    body.insertBefore(shell, body.firstChild);
    shell.querySelector("#page-root").appendChild(content);
    document.getElementById("device-power")?.addEventListener("click", toggleListening);
    document.getElementById("toggle-cockpit")?.addEventListener("click", toggleCockpit);
    document.getElementById("camera-toggle")?.addEventListener("click", toggleCamera);
    document.getElementById("refresh-cockpit-log")?.addEventListener("click", () => refreshLogs(true));
    document.querySelectorAll("[data-cockpit-log]").forEach((button) => button.addEventListener("click", () => selectLogTab(button.dataset.cockpitLog)));
    window.addEventListener("pagehide", cleanup);
    document.addEventListener("visibilitychange", () => { if (document.hidden) stopCamera(); });
  }

  function toggleCockpit() {
    const collapsed = document.body.classList.toggle("cockpit-collapsed");
    const button = document.getElementById("toggle-cockpit");
    if (!button) return;
    button.setAttribute("aria-label", collapsed ? "Expand device cockpit" : "Collapse device cockpit");
    button.setAttribute("title", collapsed ? "Expand device cockpit" : "Collapse device cockpit");
    button.innerHTML = `<i data-lucide="${collapsed ? "panel-top-open" : "panel-top-close"}"></i>`;
    icons(button);
  }

  function asrState(asr, healthy) {
    if (!healthy) return { label: "Offline", className: "offline" };
    if (asr && asr.assistant_speaking) return { label: "Speaking", className: "speaking" };
    const mode = String(asr && (asr.mode || asr.streaming_backend) || "").toLowerCase();
    if (asr && asr.listening && asr.live_capture_enabled === false && mode !== "live-captions") return { label: "Mic Offline", className: "offline" };
    if (asr && asr.listening && mode === "gemini-live" && asr.gemini_live_connected === false) {
      return asr.last_error ? { label: "Gemini Offline", className: "offline" } : { label: "Gemini Connecting", className: "paused" };
    }
    if (asr && asr.listening) return { label: "Listening", className: "listening" };
    return { label: "Paused", className: "paused" };
  }

  function renderStatus(health, asr) {
    const healthy = Boolean(health && health.status === "ok");
    const visual = asrState(asr, healthy);
    const status = document.getElementById("device-state");
    if (status) { status.textContent = visual.label; status.className = `device-state ${visual.className}`.trim(); }
    const button = document.getElementById("device-power");
    if (button) {
      button.disabled = !healthy;
      button.classList.toggle("active", Boolean(asr && asr.listening));
      button.setAttribute("aria-label", asr && asr.listening ? "Pause listening" : "Start listening");
      button.setAttribute("title", asr && asr.listening ? "Pause listening" : "Start listening");
      button.setAttribute("aria-pressed", String(Boolean(asr && asr.listening)));
    }
    const mode = asr && (asr.mode || asr.streaming_backend);
    const runtime = document.getElementById("runtime-label");
    if (runtime) runtime.textContent = healthy ? `Runtime online${mode ? ` / ${mode}` : ""}${mode === "gemini-live" ? asr && asr.gemini_live_connected ? " / connected" : " / not connected" : ""}` : "Runtime offline";
    const provider = asr && (asr.tts_backend || asr.streaming_backend || asr.mode);
    const badge = document.getElementById("provider-badge");
    const providerReady = healthy && !(mode === "gemini-live" && asr && asr.gemini_live_connected === false);
    if (badge) { badge.textContent = provider || "Provider"; badge.className = `status-badge ${providerReady ? "ok" : "error"}`; }
    const dbfs = Number(asr && asr.input_level_dbfs);
    const level = Number.isFinite(dbfs) ? Math.max(0, Math.min(100, ((dbfs + 72) / 66) * 100)) : 0;
    const micMeter = document.getElementById("microphone-meter");
    if (micMeter) micMeter.style.width = `${level}%`;
    const mic = document.getElementById("microphone-value");
    if (mic) {
      mic.textContent = !healthy ? "Unavailable" : asr && asr.input_device_name ? asr.input_device_name : asr && asr.listening ? "Listening" : "Paused";
      const connection = mode === "gemini-live" ? asr && asr.gemini_live_connected ? "; Gemini connected" : "; Gemini not connected" : "";
      mic.title = Number.isFinite(dbfs) ? `${dbfs.toFixed(1)} dBFS${connection}` : `Input level unavailable${connection}`;
    }
    const speaking = Boolean(asr && asr.assistant_speaking);
    const voice = document.getElementById("voice-value");
    const voiceMeter = document.getElementById("voice-meter");
    if (voice) voice.textContent = speaking ? "Speaking" : provider || "Idle";
    if (voiceMeter) voiceMeter.style.width = speaking ? "78%" : "0%";
    window.dispatchEvent(new CustomEvent("rachel:status", { detail: { health, asr, healthy, state: visual.label.toLowerCase() } }));
  }

  async function refreshStatus(showFeedback) {
    try {
      const results = await Promise.all([api("/healthz"), api("/api/asr")]);
      state.latestHealth = results[0]; state.latestAsr = results[1];
      renderStatus(results[0], results[1]);
      if (showFeedback) toast("Device status refreshed", "success");
    } catch (error) {
      state.latestHealth = null; renderStatus(null, state.latestAsr);
      if (showFeedback) toast(`Unable to reach runtime: ${error.message}`, "error");
    }
  }

  async function toggleListening() {
    const button = document.getElementById("device-power");
    const action = state.latestAsr && state.latestAsr.listening ? "pause_listening" : "start_listening";
    setBusy(button, true);
    try {
      state.latestAsr = await api("/api/asr", { method: "POST", body: { action } });
      renderStatus(state.latestHealth, state.latestAsr);
      toast(action === "start_listening" ? "Listening started" : "Listening paused", "success");
    } catch (error) { toast(`Unable to change listening state: ${error.message}`, "error"); }
    finally { setBusy(button, false); }
  }

  async function cameraHeartbeat() {
    try { await api("/api/camera/ping", { method: "POST" }); }
    catch (error) { stopCamera("Camera Unavailable"); toast(`Camera unavailable: ${error.message}`, "error"); }
  }

  function startCamera() {
    const image = document.getElementById("camera-preview");
    const placeholder = document.getElementById("camera-placeholder");
    if (!image || !placeholder) return;
    state.cameraActive = true;
    placeholder.hidden = false; placeholder.querySelector("span").textContent = "Connecting";
    image.hidden = false;
    image.onload = () => { if (state.cameraActive) placeholder.hidden = true; };
    image.onerror = () => { if (state.cameraActive) stopCamera("Camera Unavailable"); };
    image.src = `/camera.mjpg?operator=1&t=${Date.now()}`;
    document.getElementById("camera-toggle").setAttribute("aria-label", "Turn camera preview off");
    cameraHeartbeat(); state.cameraTimer = window.setInterval(cameraHeartbeat, 5000);
  }

  function stopCamera(label) {
    state.cameraActive = false; window.clearInterval(state.cameraTimer); state.cameraTimer = 0;
    const image = document.getElementById("camera-preview");
    const placeholder = document.getElementById("camera-placeholder");
    const button = document.getElementById("camera-toggle");
    if (!image || !placeholder || !button) return;
    image.onload = null; image.onerror = null; image.removeAttribute("src"); image.hidden = true;
    placeholder.hidden = false;
    placeholder.innerHTML = `<i data-lucide="camera-off"></i><span>${escapeHtml(label || "Camera Off")}</span>`;
    button.setAttribute("aria-label", "Turn camera preview on"); icons(placeholder);
  }

  function toggleCamera() { if (state.cameraActive) stopCamera(); else startCamera(); }

  function roleOf(entry) {
    const source = String(entry && entry.source || "").toLowerCase();
    let role = String(entry && entry.role || "user").toLowerCase();
    if (source.includes("tester_panel") || role === "wizard") role = "wizard";
    if (role === "assistant") role = "rachel";
    return role;
  }

  function isEvent(entry) {
    const role = roleOf(entry); const level = String(entry && entry.level || "").toLowerCase();
    return ["wizard", "system", "error"].includes(role) || level === "error";
  }

  function renderLogs() {
    const root = document.getElementById("cockpit-log");
    if (!root) return;
    const visible = state.logEntries.filter((entry) => state.logTab === "events" ? isEvent(entry) : !isEvent(entry)).slice(-24);
    root.innerHTML = visible.length ? visible.map((entry) => {
      const date = new Date(entry.timestamp || "");
      const time = Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const error = isEvent(entry) && (String(entry.level || "").toLowerCase() === "error" || roleOf(entry) === "error");
      return `<div class="compact-log-row ${error ? "error" : ""}"><span class="compact-log-dot"></span><span>${escapeHtml(entry.message || "")}</span><span class="compact-log-time">${escapeHtml(time)}</span></div>`;
    }).join("") : `<div class="muted">No ${escapeHtml(state.logTab)} entries yet.</div>`;
    root.scrollTop = root.scrollHeight;
  }

  function applyLogPayload(payload) {
    if (!payload || typeof payload !== "object") return;
    const version = Number(payload.version);
    if (Number.isFinite(version) && version === state.logVersion) return;
    if (Number.isFinite(version)) state.logVersion = version;
    state.logEntries = Array.isArray(payload.entries) ? payload.entries : [];
    renderLogs();
  }

  async function refreshLogs(showError) {
    try { applyLogPayload(await api("/api/logs")); }
    catch (error) { if (showError) toast(`Unable to load session log: ${error.message}`, "error"); }
  }

  function startLogs() {
    if (!document.getElementById("cockpit-log")) return;
    refreshLogs(false);
    if (typeof EventSource === "undefined") { state.logTimer = window.setInterval(() => refreshLogs(false), 1500); return; }
    state.logSource = new EventSource("/api/logs/stream");
    state.logSource.onmessage = (event) => { try { applyLogPayload(JSON.parse(event.data)); } catch (_) { /* Ignore malformed events. */ } };
    state.logSource.onerror = () => {
      state.logSource.close(); state.logSource = null;
      if (!state.logTimer) state.logTimer = window.setInterval(() => refreshLogs(false), 1500);
    };
  }

  function selectLogTab(tab) {
    state.logTab = tab === "events" ? "events" : "conversation";
    document.querySelectorAll("[data-cockpit-log]").forEach((button) => {
      const active = button.dataset.cockpitLog === state.logTab;
      button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active));
    });
    renderLogs();
  }

  function cleanup() {
    stopCamera(); window.clearInterval(state.healthTimer); window.clearInterval(state.logTimer);
    if (state.logSource) state.logSource.close();
  }

  window.RachelUI = { api, confirm: confirmAction, escapeHtml, icons, refreshStatus, setBusy, state, toast, theme: window.RachelTheme };
  mountShell(); icons(); refreshStatus(false); startLogs();
  state.healthTimer = window.setInterval(() => refreshStatus(false), 2500);
}());
