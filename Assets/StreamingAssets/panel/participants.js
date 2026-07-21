(function () {
  "use strict";
  const ui = window.RachelUI;
  const $ = (id) => document.getElementById(id);
  let state = {};
  let selectedId = "";

  function formatTime(value) {
    const number = Number(value || 0);
    if (!number) return "";
    const milliseconds = number < 100000000000 ? number * 1000 : number;
    return new Date(milliseconds).toLocaleString();
  }

  function users() { return Array.isArray(state.users) ? state.users : []; }

  function renderList() {
    const list = users();
    $("participant-count").textContent = `${list.length} participant${list.length === 1 ? "" : "s"}`;
    $("participant-list").innerHTML = list.length ? list.map((participant) => `<button class="user ${participant.user_id === selectedId ? "active" : ""}" type="button" data-user-id="${ui.escapeHtml(participant.user_id)}"><span class="user-name">${ui.escapeHtml(participant.display_name || participant.user_id)}</span><span class="user-meta">${ui.escapeHtml(participant.user_id)} · ${Number(participant.dialog_turn_count || 0)} saved turns</span></button>`).join("") : `<div class="empty-state">No participant profiles yet.</div>`;
    $("participant-list").querySelectorAll("[data-user-id]").forEach((button) => button.addEventListener("click", () => selectParticipant(button.dataset.userId)));
  }

  function renderDetail() {
    const profile = state.selected_profile && typeof state.selected_profile === "object" ? state.selected_profile : null;
    const empty = $("participant-empty");
    const detail = $("participant-detail");
    if (!selectedId || !profile) { empty.hidden = false; detail.hidden = true; return; }
    empty.hidden = true;
    detail.hidden = false;
    const turns = Array.isArray(profile.dialog_turns) ? profile.dialog_turns : [];
    const displayName = String(profile.display_name || selectedId);
    $("participant-title").textContent = displayName;
    $("participant-subtitle").textContent = "Independent profile and memory";
    $("participant-id").textContent = selectedId;
    $("participant-name").value = displayName;
    $("turn-count").textContent = String(turns.length);
    $("voiceprint-state").textContent = state.selected_speaker_profile && state.selected_speaker_profile.has_profile ? "Enrolled" : "Not enrolled";
    $("participant-history").innerHTML = turns.length ? turns.map((turn) => {
      const assistant = String(turn.role || "user").toLowerCase() === "assistant";
      return `<article class="turn"><div class="role ${assistant ? "assistant" : ""}">${assistant ? "Rachel" : "Participant"}</div><div>${ui.escapeHtml(turn.text || "")}</div><div class="time">${ui.escapeHtml(formatTime(turn.ts))}</div></article>`;
    }).join("") : `<div class="empty-state">No saved conversation yet.</div>`;
    $("participant-history").scrollTop = $("participant-history").scrollHeight;
  }

  async function loadParticipants(showFeedback) {
    try {
      state = await ui.api(`/api/memory?user_id=${encodeURIComponent(selectedId)}&_ts=${Date.now()}`);
      if (selectedId && !state.selected_profile) selectedId = "";
      renderList();
      renderDetail();
      if (showFeedback) ui.toast("Participant history refreshed", "success");
    } catch (error) { ui.toast(`Unable to load participants: ${error.message}`, "error"); }
  }

  async function selectParticipant(id) { selectedId = id; await loadParticipants(false); }

  async function createParticipant(event) {
    event.preventDefault();
    const input = $("new-participant-name");
    const name = input.value.trim();
    if (!name) return;
    if (!await ui.confirm({ title: "Add participant", message: `Create a participant profile for ${name}?`, confirmLabel: "Create profile" })) return;
    const button = $("create-participant");
    ui.setBusy(button, true, "Adding...");
    try {
      let payload = await ui.api("/api/memory", { method: "POST", body: { action: "create_user" } });
      selectedId = payload.selected_user_id;
      state = await ui.api("/api/memory", { method: "POST", body: { action: "update_user_raw", user_id: selectedId, profile: { display_name: name, dialog_turns: [] } } });
      input.value = "";
      renderList(); renderDetail();
      ui.toast("Participant created", "success");
    } catch (error) { ui.toast(`Unable to create participant: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }

  async function saveName(event) {
    event.preventDefault();
    if (!selectedId) return;
    const profile = state.selected_profile || {};
    const displayName = $("participant-name").value.trim() || selectedId;
    const button = $("save-participant-name");
    ui.setBusy(button, true, "Saving...");
    try {
      state = await ui.api("/api/memory", { method: "POST", body: { action: "update_user_raw", user_id: selectedId, profile: { display_name: displayName, dialog_turns: Array.isArray(profile.dialog_turns) ? profile.dialog_turns : [] } } });
      renderList(); renderDetail();
      ui.toast("Participant name saved", "success");
    } catch (error) { ui.toast(`Unable to save participant: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }

  async function deleteParticipant() {
    if (!selectedId) return;
    const displayName = $("participant-name").value.trim() || selectedId;
    if (!await ui.confirm({ title: "Delete participant", message: `Delete ${displayName}, including conversation history and voiceprint?`, confirmLabel: "Delete participant", danger: true })) return;
    const button = $("delete-participant");
    ui.setBusy(button, true, "Deleting...");
    try {
      state = await ui.api("/api/memory", { method: "POST", body: { action: "delete_user", user_id: selectedId } });
      selectedId = "";
      renderList(); renderDetail();
      ui.toast("Participant deleted", "success");
    } catch (error) { ui.toast(`Unable to delete participant: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }

  $("refresh-participants").addEventListener("click", () => loadParticipants(true));
  $("new-participant-form").addEventListener("submit", createParticipant);
  $("participant-name-form").addEventListener("submit", saveName);
  $("delete-participant").addEventListener("click", deleteParticipant);
  loadParticipants(false);
  ui.icons();
}());
