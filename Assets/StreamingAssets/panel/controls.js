(function () {
  "use strict";
  const ui = window.RachelUI;
  const $ = (id) => document.getElementById(id);
  let voiceOptions = {};
  let cameraActive = false;
  let cameraTimer = 0;
  let faceResetTimer = 0;
  const facePreviewAssets = {
    neutral: "/panel-assets/face-neutral.png",
    happy: "/panel-assets/face-happy.png",
    excited: "/panel-assets/face-excited.png",
    sad: "/panel-assets/face-sad.png",
    verysad: "/panel-assets/face-verysad.png"
  };
  const emoji = {
    neutral: "&#128528;", happy: "&#128522;", excited: "&#129321;", sad: "&#128577;",
    verysad: "&#128557;", confused: "&#128533;", concerned: "&#128543;", upset: "&#128548;",
    angry: "&#128545;", surprised: "&#128562;", tired: "&#128554;"
  };

  function setView() {
    const view = location.hash.toLowerCase() === "#expressions" ? "expressions" : "controller";
    $("expressions-view").hidden = view !== "expressions";
    $("controller-view").hidden = view !== "controller";
    $("controls-title").textContent = view === "expressions" ? "Expressions" : "Controller";
    $("controls-description").textContent = view === "expressions"
      ? "Send face presets already supported by Rachel's avatar."
      : "Operate the hardware and services Rachel actually supports.";
    document.querySelectorAll("[data-control-view]").forEach((link) => link.classList.toggle("active", link.dataset.controlView === view));
  }

  function selectOptions(element, values, current) {
    const items = Array.from(new Set((values || []).map((value) => String(value || "").trim()).filter(Boolean)));
    element.innerHTML = items.length
      ? items.map((value) => `<option value="${ui.escapeHtml(value)}">${ui.escapeHtml(value)}</option>`).join("")
      : `<option value="">Unavailable</option>`;
    if (current && items.includes(current)) element.value = current;
  }

  function kokoroVoices(payload) {
    return (payload && (payload.voices || payload.options || payload.available)) || voiceOptions.kokoroVoices || [];
  }

  async function sendCommand(endpoint, body, button, successMessage) {
    ui.setBusy(button, true, "Sending...");
    try {
      const payload = await ui.api(endpoint, { method: "POST", body });
      ui.toast(payload.message || successMessage || "Command sent", "success");
      return payload;
    } catch (error) {
      ui.toast(`${successMessage || "Command"} failed: ${error.message}`, "error");
      return null;
    } finally {
      ui.setBusy(button, false);
    }
  }

  function previewKey(value) {
    const key = String(value || "").trim().toLowerCase().replace(/[^a-z]/g, "");
    if (facePreviewAssets[key]) return key;
    const withoutAvatar = key.replace(/^[abcd]/, "");
    return facePreviewAssets[withoutAvatar] ? withoutAvatar : key;
  }

  async function loadExpressions() {
    try {
      const payload = await ui.api("/api/face/options");
      const presets = Array.isArray(payload.presets) ? payload.presets : [];
      const groups = { "Basic emotions": [], "Avatar A": [], "Avatar B": [], "Avatar C": [], "Avatar D": [] };
      presets.forEach((preset) => {
        const value = String(preset || "");
        const match = value.match(/^([ABCD])(.+)$/);
        if (match) groups[`Avatar ${match[1]}`].push({ value, label: match[2] });
        else groups["Basic emotions"].push({ value, label: value });
      });
      $("expression-groups").innerHTML = Object.entries(groups).filter(([, values]) => values.length).map(([name, values]) => (
        `<section><div class="panel-head"><h2>${ui.escapeHtml(name)}</h2><span class="muted">${values.length}</span></div>` +
        `<div class="expression-grid">${values.map((item) => {
          const key = previewKey(item.label);
          const image = facePreviewAssets[key];
          return `<button class="expression-button" type="button" data-face="${ui.escapeHtml(item.value)}" title="Send ${ui.escapeHtml(item.value)}">` +
            (image ? `<img class="expression-image" src="${image}" alt="">` : `<span class="expression-emoji">${emoji[key] || "&#128578;"}</span>`) +
            `<span class="expression-label">${ui.escapeHtml(item.label)}</span></button>`;
        }).join("")}</div></section>`
      )).join("") || `<div class="empty-state">No expression presets are available.</div>`;
      $("expression-groups").querySelectorAll("[data-face]").forEach((button) => button.addEventListener("click", () => sendFace(button.dataset.face, button)));
      showFacePreview("neutral", 0);
    } catch (error) {
      $("expression-groups").innerHTML = `<div class="notice error">Unable to load expressions: ${ui.escapeHtml(error.message)}</div>`;
    }
  }

  function showFacePreview(value, durationSeconds) {
    const key = previewKey(value);
    const activePreset = String(value || "").trim().toLowerCase().replace(/\s+/g, "");
    const source = facePreviewAssets[key];
    if (!source) return;
    const preview = $("device-face-preview");
    if (!preview) return;
    window.clearTimeout(faceResetTimer);
    faceResetTimer = 0;
    preview.src = source;
    preview.alt = `Rachel ${key === "verysad" ? "very sad" : key} expression`;
    document.querySelectorAll("[data-face]").forEach((item) => {
      const itemPreset = String(item.dataset.face || "").trim().toLowerCase().replace(/\s+/g, "");
      item.classList.toggle("active", itemPreset === activePreset);
    });
    if (key !== "neutral" && durationSeconds > 0) {
      faceResetTimer = window.setTimeout(() => showFacePreview("neutral", 0), durationSeconds * 1000);
    }
  }

  async function sendFace(value, button) {
    const preset = String(value || "").trim().replace(/\s+/g, "");
    if (!preset) {
      ui.toast("Enter an expression preset", "error");
      return;
    }
    const seconds = Number($("face-seconds").value) || 3;
    const result = await sendCommand("/api/face", { mode: preset, seconds }, button, "Expression sent");
    if (result) showFacePreview(preset, seconds);
  }

  async function refreshVoices(showFeedback) {
    const badge = $("voice-status");
    badge.textContent = "Loading";
    try {
      voiceOptions = await ui.api("/api/voice/options");
      selectOptions($("tts-backend"), voiceOptions.backends && voiceOptions.backends.length ? voiceOptions.backends : ["piper", "kokoro"], voiceOptions.backendCurrent);
      selectOptions($("tts-model"), voiceOptions.models || [], voiceOptions.modelCurrent);
      await refreshVoicesForBackend();
      badge.textContent = "Ready";
      badge.className = "status-badge ok";
      if (showFeedback) ui.toast("Voice options refreshed", "success");
    } catch (error) {
      badge.textContent = "Unavailable";
      badge.className = "status-badge error";
      if (showFeedback) ui.toast(`Unable to load voice options: ${error.message}`, "error");
    }
  }

  async function refreshVoicesForBackend() {
    const backend = $("tts-backend").value || voiceOptions.backendCurrent || "piper";
    $("tts-model").disabled = backend !== "piper";
    if (backend === "kokoro") {
      try {
        const payload = await ui.api("/api/kokoro/options");
        selectOptions($("tts-voice"), kokoroVoices(payload), payload.current || payload.voice || voiceOptions.kokoroVoiceCurrent);
      } catch (_) {
        selectOptions($("tts-voice"), kokoroVoices({}), voiceOptions.kokoroVoiceCurrent);
      }
    } else if (backend === "google-cloud") {
      selectOptions($("tts-voice"), voiceOptions.googleCloudVoices || [], voiceOptions.googleCloudVoiceCurrent);
    } else {
      selectOptions($("tts-voice"), voiceOptions.voices || [], voiceOptions.current);
    }
  }

  async function applyBackend() {
    const backend = $("tts-backend").value;
    await sendCommand("/api/voice", { action: "set_backend", backend }, $("tts-backend"), "TTS backend updated");
    await refreshVoicesForBackend();
  }

  async function applyVoice() {
    const backend = $("tts-backend").value;
    const voice = $("tts-voice").value;
    const action = backend === "kokoro" ? "set_kokoro_voice" : backend === "google-cloud" ? "set_google_cloud_voice" : "set";
    if (voice) await sendCommand("/api/voice", { action, voice }, $("tts-voice"), "Voice updated");
  }

  async function speak() {
    const text = $("speak-text").value.trim();
    if (!text) {
      ui.toast("Enter text for Rachel to speak", "error");
      return;
    }
    await sendCommand("/api/speak", {
      text,
      voice: $("tts-voice").value,
      model: $("tts-model").value,
      backend: $("tts-backend").value,
      speed: Number($("voice-speed").value) || 1,
      volume: Number($("voice-volume").value) || 1
    }, $("speak-now"), "Speech requested");
  }

  function updateVolumeOutput() {
    $("voice-volume-value").value = Number($("voice-volume").value || 1).toFixed(1);
  }

  function setCameraUi(active, label) {
    cameraActive = active;
    $("start-camera").disabled = active;
    $("stop-camera").disabled = !active;
    $("describe-camera").disabled = !active;
    $("camera-state").textContent = label || (active ? "Preview on" : "Camera off");
    $("camera-state").className = `status-badge ${active ? "ok" : ""}`;
  }

  async function heartbeat() {
    try {
      await ui.api("/api/camera/ping", { method: "POST" });
    } catch (error) {
      stopCamera("Unavailable");
      ui.toast(`Camera unavailable: ${error.message}`, "error");
    }
  }

  function startCamera() {
    const image = $("control-camera");
    const placeholder = $("control-camera-placeholder");
    setCameraUi(true, "Connecting");
    placeholder.hidden = false;
    placeholder.querySelector("span").textContent = "Connecting";
    image.hidden = false;
    image.onload = () => { if (cameraActive) { placeholder.hidden = true; setCameraUi(true, "Preview on"); } };
    image.onerror = () => { if (cameraActive) stopCamera("Unavailable"); };
    image.src = `/camera.mjpg?controls=1&t=${Date.now()}`;
    heartbeat();
    cameraTimer = window.setInterval(heartbeat, 5000);
  }

  function stopCamera(label) {
    cameraActive = false;
    window.clearInterval(cameraTimer);
    cameraTimer = 0;
    const image = $("control-camera");
    image.onload = null;
    image.onerror = null;
    image.removeAttribute("src");
    image.hidden = true;
    const placeholder = $("control-camera-placeholder");
    placeholder.hidden = false;
    placeholder.innerHTML = `<i data-lucide="camera-off"></i><span>${ui.escapeHtml(label || "Camera Off")}</span>`;
    setCameraUi(false, label || "Camera off");
    ui.icons(placeholder);
  }

  async function describeCamera() {
    if (!cameraActive) return;
    const result = $("vision-result");
    result.textContent = "Analyzing the current frame...";
    ui.setBusy($("describe-camera"), true, "Analyzing...");
    try {
      const payload = await ui.api("/api/vision/describe", { method: "POST", body: { prompt: $("vision-prompt").value.trim(), model: $("vision-model").value.trim() } });
      result.textContent = payload.description || payload.response || payload.message || JSON.stringify(payload, null, 2);
    } catch (error) {
      result.textContent = `Error: ${error.message}`;
      ui.toast(`Vision request failed: ${error.message}`, "error");
    } finally {
      ui.setBusy($("describe-camera"), false);
    }
  }

  async function sendLed(button) {
    const settings = {
      mode: button.dataset.led,
      color: $("led-color").value,
      brightness: Number($("led-brightness").value) || 0.8,
      period: Number($("led-period").value) || 2,
      duration: Number($("led-duration").value) || 0
    };
    await sendCommand("/api/led", settings, button, "LED command sent");
  }

  async function sendFlower(button) {
    const action = button.dataset.flower;
    await sendCommand("/api/flower", { action }, button, "Flower command sent");
  }

  function customFaceValue() {
    const name = $("face-custom").value.trim().replace(/\s+/g, "");
    return `${$("face-custom-avatar").value}${name}`;
  }

  window.addEventListener("hashchange", setView);
  $("refresh-controls").addEventListener("click", () => refreshVoices(true));
  $("tts-backend").addEventListener("change", applyBackend);
  $("tts-voice").addEventListener("change", applyVoice);
  $("tts-model").addEventListener("change", (event) => sendCommand("/api/voice", { action: "set_model", model: event.target.value }, event.target, "TTS model updated"));
  $("voice-volume").addEventListener("input", updateVolumeOutput);
  $("speak-now").addEventListener("click", speak);
  $("send-custom-face").addEventListener("click", () => sendFace(customFaceValue(), $("send-custom-face")));
  $("start-camera").addEventListener("click", startCamera);
  $("stop-camera").addEventListener("click", () => stopCamera());
  $("describe-camera").addEventListener("click", describeCamera);
  document.querySelectorAll("[data-led]").forEach((button) => button.addEventListener("click", () => sendLed(button)));
  document.querySelectorAll("[data-flower]").forEach((button) => button.addEventListener("click", () => sendFlower(button)));
  window.addEventListener("pagehide", () => {
    stopCamera();
    window.clearTimeout(faceResetTimer);
  });
  setView();
  updateVolumeOutput();
  loadExpressions();
  refreshVoices(false);
  setCameraUi(false);
  ui.icons();
}());
