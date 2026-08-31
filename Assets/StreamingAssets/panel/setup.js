(function () {
  "use strict";
  const ui = window.RachelUI;
  const $ = (id) => document.getElementById(id);
  let prereq = null;
  let asr = null;
  let runtime = null;

  function setBadge(id, label, kind) { const element = $(id); element.textContent = label; element.className = `status-badge ${kind || ""}`; }
  function setStep(id, done) { $(id).classList.toggle("done", Boolean(done)); }

  function recognitionProblem() {
    const mode = $("setup-asr-mode").value || String(asr && asr.mode || "");
    if (!asr) return "Speech runtime is unavailable.";
    if (mode === "live-captions" && !asr.live_captions_available) return asr.live_captions_error || "Live Captions listener is not installed. Choose Gemini Live or install EnableLcMic.exe.";
    if (mode !== "live-captions" && !asr.sounddevice_available) return "Python sounddevice is not installed.";
    if (mode !== "live-captions" && !asr.live_capture_enabled) return asr.last_error || "The selected microphone could not be opened.";
    if (mode === "gemini-live" && !asr.gemini_live_connected) return asr.last_error || "Gemini Live is connecting or unavailable. Check the Gemini key, model and network connection.";
    return "";
  }

  function renderReadiness() {
    const piperReady = Boolean(prereq && prereq.piper_ready);
    const ollamaReady = Boolean(prereq && prereq.ollama_running && prereq.ollama_model_available);
    const problem = recognitionProblem();
    const recognitionReady = Boolean(asr && asr.status === "ok" && !problem);
    $("setup-microphone-status").textContent = problem || `Active input: ${asr.input_device_name || "Windows default"}; level ${Number(asr.input_level_dbfs || -96).toFixed(1)} dBFS.`;
    $("setup-microphone-status").className = `notice full${problem ? " error" : ""}`;
    $("piper-status").textContent = piperReady ? "Ready" : "Needs setup";
    $("piper-model-path").value = String(prereq && prereq.piper_model_path || "");
    $("ollama-status").textContent = prereq && prereq.ollama_running ? "Running" : prereq && prereq.ollama_installed ? "Installed" : "Not installed";
    $("ollama-model-ready").textContent = prereq && prereq.ollama_model_available ? "Yes" : "No";
    $("ollama-hint").value = String(prereq && (prereq.ollama_error || (prereq.needs_ollama_setup ? "Install Ollama and pull the selected model" : "Ready")) || "");
    setBadge("dependency-status", piperReady && ollamaReady ? "Passed" : "Action needed", piperReady && ollamaReady ? "ok" : "warn");
    setBadge("recognition-status", recognitionReady ? "Passed" : "Unavailable", recognitionReady ? "ok" : "error");
    setStep("step-dependencies", piperReady && ollamaReady);
    setStep("step-recognition", recognitionReady);
    setStep("step-cloud", $("setup-asr-mode").value !== "api" || Boolean($("setup-openai-key").value));
    setStep("step-intent", true);
    const completed = [piperReady && ollamaReady, recognitionReady, $("setup-asr-mode").value !== "api" || Boolean($("setup-openai-key").value), true].filter(Boolean).length;
    const percent = Math.round((completed / 4) * 100);
    $("setup-progress").style.width = `${percent}%`;
    $("setup-progress-label").textContent = `${completed} of 4 readiness checks passed`;
    setBadge("setup-summary", percent === 100 ? "Ready" : `${completed}/4 passed`, percent === 100 ? "ok" : "warn");
  }

  function loadFields() {
    const modes = Array.isArray(asr.available_modes) ? asr.available_modes : [];
    $("setup-asr-mode").innerHTML = modes.map((mode) => `<option value="${ui.escapeHtml(mode)}">${ui.escapeHtml(mode)}</option>`).join("");
    $("setup-asr-mode").value = asr.mode || modes[0] || "api";
    $("setup-listening").checked = Boolean(asr.listening);
    const devices = Array.isArray(asr.input_devices) ? asr.input_devices : [];
    const configuredInput = String(runtime.input_device_name || "");
    $("setup-microphone").innerHTML = `<option value="">Windows default microphone</option>` + devices.map((device) => `<option value="${ui.escapeHtml(device.name || "")}">${ui.escapeHtml(device.name || "Input")}${device.hostapi ? ` (${ui.escapeHtml(device.hostapi)})` : ""}</option>`).join("");
    if (configuredInput && !devices.some((device) => String(device.name || "") === configuredInput)) $("setup-microphone").insertAdjacentHTML("beforeend", `<option value="${ui.escapeHtml(configuredInput)}">${ui.escapeHtml(configuredInput)} (not currently detected)</option>`);
    $("setup-microphone").value = configuredInput;
    $("setup-openai-key").value = runtime.openai_api_key || "";
    $("setup-openai-model").value = runtime.openai_transcribe_model || "";
    $("setup-openai-url").value = runtime.openai_base_url || "";
    $("setup-openai-prompt").value = runtime.openai_transcribe_prompt || "";
    $("ollama-model").value = runtime.ollama_model || prereq.ollama_model || "qwen3.5:0.8b";
    $("setup-launch-triggers").value = runtime.launch_triggers || "";
    $("setup-exit-keywords").value = runtime.exit_keywords || "";
    $("setup-llm-intent").checked = Boolean(runtime.use_llm_intent_classifier);
    $("setup-moonshine-intent").checked = Boolean(runtime.use_moonshine_intent_recognizer);
    $("setup-intent-manifest").value = runtime.intent_manifest_path || "";
    $("setup-game-manifest").value = runtime.game_manifest_path || "";
    setBadge("cloud-status", $("setup-asr-mode").value === "api" ? (runtime.openai_api_key_set ? "Configured" : "Required") : "Optional", $("setup-asr-mode").value === "api" ? (runtime.openai_api_key_set ? "ok" : "warn") : "");
  }

  async function reload(showFeedback) {
    try {
      [asr, runtime, prereq] = await Promise.all([ui.api("/api/asr"), ui.api("/api/runtime/config"), ui.api("/api/runtime/prereq")]);
      loadFields(); renderReadiness();
      $("setup-status").textContent = "Readiness checks complete.";
      if (showFeedback) ui.toast("Setup checks complete", "success");
    } catch (error) { $("setup-status").textContent = `Error: ${error.message}`; ui.toast(`Setup check failed: ${error.message}`, "error"); }
  }

  async function ollamaAction(action) {
    const map = { install: "install-ollama", pull_model: "pull-model", open_download: "open-ollama" };
    const button = $(map[action]);
    ui.setBusy(button, true, action === "pull_model" ? "Starting pull..." : "Working...");
    try {
      const body = { action };
      if (action === "pull_model") body.model = $("ollama-model").value.trim() || "qwen3.5:0.8b";
      const data = await ui.api("/api/runtime/ollama", { method: "POST", body });
      ui.toast(data.message || "Ollama action started", "success");
      if (action !== "open_download") await reload(false);
    } catch (error) { ui.toast(`Ollama action failed: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }

  async function save(event) {
    event.preventDefault();
    const button = $("save-setup");
    ui.setBusy(button, true, "Saving...");
    try {
      await ui.api("/api/runtime/config", { method: "POST", body: {
        openai_api_key: $("setup-openai-key").value.trim(), openai_transcribe_model: $("setup-openai-model").value.trim(), openai_base_url: $("setup-openai-url").value.trim(), openai_transcribe_prompt: $("setup-openai-prompt").value.trim(),
        ollama_model: $("ollama-model").value.trim(), input_device_name: $("setup-microphone").value, launch_triggers: $("setup-launch-triggers").value.trim(), exit_keywords: $("setup-exit-keywords").value.trim(),
        use_llm_intent_classifier: $("setup-llm-intent").checked, use_moonshine_intent_recognizer: $("setup-moonshine-intent").checked,
        intent_manifest_path: $("setup-intent-manifest").value.trim(), game_manifest_path: $("setup-game-manifest").value.trim()
      } });
      await ui.api("/api/asr", { method: "POST", body: { action: "set_mode", mode: $("setup-asr-mode").value } });
      await ui.api("/api/asr", { method: "POST", body: { action: "set_listening", listening: $("setup-listening").checked } });
      setBadge("save-status", "Saved", "ok");
      $("step-save").classList.add("done");
      $("setup-status").textContent = "Setup saved and applied to the live runtime.";
      ui.toast("Setup saved", "success");
      await reload(false);
    } catch (error) { setBadge("save-status", "Failed", "error"); $("setup-status").textContent = `Error: ${error.message}`; ui.toast(`Unable to save setup: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }

  $("reload-setup").addEventListener("click", () => reload(true));
  $("install-ollama").addEventListener("click", () => ollamaAction("install"));
  $("pull-model").addEventListener("click", () => ollamaAction("pull_model"));
  $("open-ollama").addEventListener("click", () => ollamaAction("open_download"));
  $("setup-asr-mode").addEventListener("change", () => { setBadge("cloud-status", $("setup-asr-mode").value === "api" ? ($("setup-openai-key").value ? "Configured" : "Required") : "Optional", $("setup-asr-mode").value === "api" ? ($("setup-openai-key").value ? "ok" : "warn") : ""); renderReadiness(); });
  $("setup-microphone").addEventListener("change", renderReadiness);
  $("setup-openai-key").addEventListener("input", renderReadiness);
  $("setup-form").addEventListener("submit", save);
  reload(false);
  ui.icons();
}());
