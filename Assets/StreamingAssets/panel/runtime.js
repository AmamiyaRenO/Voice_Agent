(function () {
  "use strict";
  const ui = window.RachelUI;
  const $ = (id) => document.getElementById(id);
  const textFields = {
    "intent-manifest": "intent_manifest_path", "game-manifest": "game_manifest_path", "ollama-model": "ollama_model",
    "openai-response-model": "openai_response_model", "gemini-response-model": "gemini_response_model", "gemini-live-model": "gemini_live_model",
    "gemini-live-voice": "gemini_live_voice", "kokoro-voice": "kokoro_voice", "kokoro-language": "kokoro_lang_code",
    "openai-key": "openai_api_key", "gemini-key": "gemini_api_key", "openai-transcribe-model": "openai_transcribe_model",
    "openai-base-url": "openai_base_url", "openai-transcribe-prompt": "openai_transcribe_prompt", "launch-triggers": "launch_triggers", "exit-keywords": "exit_keywords"
  };
  const selectFields = {
    "conversation-pipeline": "conversation_pipeline_mode", "conversation-profile": "conversation_profile", "cloud-provider": "cloud_response_provider",
    "hotword-strategy": "asr_hotword_strategy", "stable-repeats": "asr_stable_partial_repeats", "tts-backend": "tts_backend"
  };
  let latestAsr = null;

  function recognitionProvider(data) {
    const values = [data.local_streaming_asr_mode, data.cloud_streaming_asr_mode];
    if (values.includes("gemini-live") || data.cloud_response_provider === "gemini" && data.conversation_profile === "cloud") return "gemini-live";
    if (values.includes("api")) return "api";
    return "live-captions";
  }

  function recognitionPayload(provider) {
    if (provider === "gemini-live") return { conversation_profile: "cloud", cloud_response_provider: "gemini", local_asr_mode: "api", cloud_asr_mode: "api", local_streaming_asr_mode: "gemini-live", cloud_streaming_asr_mode: "gemini-live" };
    if (provider === "api") return { conversation_profile: "cloud", cloud_response_provider: "openai", local_asr_mode: "api", cloud_asr_mode: "api", local_streaming_asr_mode: "api", cloud_streaming_asr_mode: "api" };
    return { local_streaming_asr_mode: "live-captions", cloud_streaming_asr_mode: "live-captions" };
  }

  function applyConfig(data) {
    Object.entries(textFields).forEach(([id, key]) => { $(id).value = String(data[key] || ""); });
    Object.entries(selectFields).forEach(([id, key]) => { $(id).value = String(data[key] ?? ""); });
    $("recognition-provider").value = recognitionProvider(data);
    $("gemini-native-response").checked = Boolean(data.gemini_live_native_response);
    $("llm-intent").checked = Boolean(data.use_llm_intent_classifier);
    $("voice-id-switch").checked = Boolean(data.speaker_id_enabled && data.speaker_auto_learning_enabled);
    $("privacy-status").textContent = $("voice-id-switch").checked ? "Voice matching and automatic learning are enabled." : "Voice matching and automatic learning are disabled.";
    $("runtime-path").textContent = `Config: ${data.path || "unknown"}`;
  }

  async function loadPrompt() {
    try {
      const data = await ui.api("/api/llm/prompt");
      $("system-prompt").value = String(data.system_prompt || data.prompt || "");
    } catch (error) {
      $("system-prompt").placeholder = `Response service unavailable: ${error.message}`;
    }
  }

  async function loadConfig(showFeedback) {
    try {
      const data = await ui.api("/api/runtime/config");
      applyConfig(data);
      await loadPrompt();
      $("runtime-status").textContent = data.message || "Configuration loaded.";
      if (showFeedback) ui.toast("Runtime configuration reloaded", "success");
    } catch (error) { $("runtime-status").textContent = `Error: ${error.message}`; ui.toast(`Unable to load runtime configuration: ${error.message}`, "error"); }
  }

  function buildPayload() {
    const payload = {};
    Object.entries(textFields).forEach(([id, key]) => { payload[key] = $(id).value.trim(); });
    Object.entries(selectFields).forEach(([id, key]) => { payload[key] = $(id).value; });
    payload.gemini_live_native_response = $("gemini-native-response").checked;
    payload.use_llm_intent_classifier = $("llm-intent").checked;
    payload.speaker_id_enabled = $("voice-id-switch").checked;
    payload.speaker_auto_learning_enabled = $("voice-id-switch").checked;
    return Object.assign(payload, recognitionPayload($("recognition-provider").value));
  }

  function renderListening(asr, healthy) {
    latestAsr = asr || latestAsr || {};
    const listening = Boolean(latestAsr.listening);
    $("listening-label").textContent = !healthy ? "Runtime offline" : listening ? "Listening is active" : "Listening is paused";
    $("listening-detail").textContent = !healthy ? "Start the desktop runtime to use speech input." : listening ? "Rachel is accepting microphone input." : "Speech input remains available when you resume.";
    const button = $("toggle-listening");
    button.disabled = !healthy;
    button.querySelector("span").textContent = listening ? "Pause Listening" : "Start Listening";
  }

  async function toggleListening() {
    const button = $("toggle-listening");
    const action = latestAsr && latestAsr.listening ? "pause_listening" : "start_listening";
    ui.setBusy(button, true, action === "start_listening" ? "Starting..." : "Pausing...");
    try {
      latestAsr = await ui.api("/api/asr", { method: "POST", body: { action } });
      ui.toast(action === "start_listening" ? "Listening started" : "Listening paused", "success");
      await ui.refreshStatus(false);
    } catch (error) { ui.toast(`Unable to change listening state: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); renderListening(latestAsr, Boolean(ui.state.latestHealth)); }
  }

  async function savePrivacy() {
    const enabled = $("voice-id-switch").checked;
    $("privacy-status").textContent = "Applying privacy setting...";
    try {
      await ui.api("/api/runtime/config", { method: "POST", body: { speaker_id_enabled: enabled, speaker_auto_learning_enabled: enabled } });
      $("privacy-status").textContent = enabled ? "Voice matching and automatic learning are enabled." : "Voice matching and automatic learning are disabled. Existing profiles were kept.";
      ui.toast(enabled ? "Voice identification enabled" : "Voice identification disabled", "success");
    } catch (error) {
      $("voice-id-switch").checked = !enabled;
      $("privacy-status").textContent = `Unable to apply setting: ${error.message}`;
      ui.toast(`Privacy setting failed: ${error.message}`, "error");
    }
  }

  async function saveConfig(event) {
    event.preventDefault();
    const button = $("save-runtime");
    ui.setBusy(button, true, "Saving...");
    try {
      const data = await ui.api("/api/runtime/config", { method: "POST", body: buildPayload() });
      const prompt = $("system-prompt").value.trim();
      if (prompt) await ui.api("/api/llm/prompt", { method: "POST", body: { prompt } });
      applyConfig(data);
      $("runtime-status").textContent = data.message || "Configuration saved.";
      ui.toast("Runtime configuration saved", "success");
      await ui.refreshStatus(false);
    } catch (error) { $("runtime-status").textContent = `Error: ${error.message}`; ui.toast(`Unable to save runtime configuration: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }

  async function resetPrompt() {
    if (!await ui.confirm({ title: "Reset system prompt", message: "Restore the response service's default conversation prompt?", confirmLabel: "Reset prompt" })) return;
    const button = $("reset-system-prompt");
    ui.setBusy(button, true, "Resetting...");
    try {
      const data = await ui.api("/api/llm/prompt", { method: "POST", body: { reset: true } });
      $("system-prompt").value = String(data.system_prompt || data.prompt || "");
      ui.toast("System prompt reset", "success");
    } catch (error) { ui.toast(`Unable to reset prompt: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }

  $("runtime-form").addEventListener("submit", saveConfig);
  $("reload-runtime").addEventListener("click", () => loadConfig(true));
  $("reset-system-prompt").addEventListener("click", resetPrompt);
  $("toggle-listening").addEventListener("click", toggleListening);
  $("voice-id-switch").addEventListener("change", savePrivacy);
  $("theme-switch").checked = window.RachelTheme && window.RachelTheme.get() === "light";
  $("theme-switch").addEventListener("change", (event) => window.RachelTheme && window.RachelTheme.set(event.target.checked ? "light" : "dark"));
  window.addEventListener("rachel:status", (event) => renderListening(event.detail.asr, event.detail.healthy));
  renderListening(ui.state.latestAsr || {}, Boolean(ui.state.latestHealth));
  loadConfig(false);
  ui.icons();
}());
