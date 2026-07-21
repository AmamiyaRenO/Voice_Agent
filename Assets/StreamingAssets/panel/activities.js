(function () {
  "use strict";
  const ui = window.RachelUI;
  const $ = (id) => document.getElementById(id);
  let games = [];
  let selected = -1;
  let manifestPath = "";
  const fields = { id: "activity-id", name: "activity-name", keywords_text: "activity-keywords", description: "activity-description", how_to_play: "activity-how", players_text: "activity-players", activity_level: "activity-level", tags_text: "activity-tags", recommendation_weight: "activity-weight", exec: "activity-exec", workdir: "activity-workdir" };

  function splitList(value) { return String(value || "").split(",").map((item) => item.trim()).filter(Boolean); }
  function parsePlayers(value) {
    const range = String(value || "").match(/^\s*(\d+)\s*-\s*(\d+)\s*$/);
    if (range) return { min: Math.max(1, Number(range[1])), max: Math.max(1, Number(range[2])) };
    const count = Number(value);
    return Number.isFinite(count) && count > 0 ? { min: Math.trunc(count), max: Math.trunc(count) } : { min: 1, max: 4 };
  }
  function normalize(game) {
    return { id: String(game.id || ""), name: String(game.name || ""), exec: String(game.exec || ""), workdir: String(game.workdir || ""), keywords_text: Array.isArray(game.keywords) ? game.keywords.join(", ") : String(game.keywords_text || ""), description: String(game.description || ""), how_to_play: String(game.how_to_play || ""), players_text: game.players_text || `${game.players_min || 1}-${game.players_max || 4}`, tags_text: Array.isArray(game.tags) ? game.tags.join(", ") : String(game.tags_text || ""), activity_level: String(game.activity_level || ""), recommendation_weight: String(game.recommendation_weight ?? "0.5"), launch_ready: game.launch_ready !== false && !game.path_error, path_error: String(game.path_error || "") };
  }
  function filteredIndexes() {
    const query = $("activity-search").value.trim().toLowerCase();
    return games.map((_, index) => index).filter((index) => !query || [games[index].id, games[index].name, games[index].keywords_text].some((value) => String(value).toLowerCase().includes(query)));
  }
  function playerLabel(game) { return game.players_text || "Not set"; }
  function renderList() {
    const indexes = filteredIndexes();
    $("activity-summary").textContent = indexes.length === games.length ? `${games.length} games` : `${indexes.length} of ${games.length}`;
    $("activity-list").innerHTML = indexes.length ? indexes.map((index) => {
      const game = games[index];
      return `<article class="activity-card ${index === selected ? "selected" : ""}"><div class="application-card-head"><span class="application-icon"><i data-lucide="gamepad-2"></i></span><span class="status-badge ${game.launch_ready ? "ok" : "error"}">${game.launch_ready ? "Ready" : "Path needed"}</span></div><div class="activity-card-copy"><h2>${ui.escapeHtml(game.name || "Untitled game")}</h2><p class="mono">${ui.escapeHtml(game.id || "No ID")}</p><p>${ui.escapeHtml(game.description || game.how_to_play || "No description has been added.")}</p><div class="application-meta"><span class="chip"><i data-lucide="users"></i>${ui.escapeHtml(playerLabel(game))}</span>${game.activity_level ? `<span class="chip"><i data-lucide="activity"></i>${ui.escapeHtml(game.activity_level)}</span>` : ""}</div>${game.path_error ? `<p class="notice error">${ui.escapeHtml(game.path_error)}</p>` : ""}</div><div class="application-actions"><button class="primary" type="button" data-launch="${index}" ${game.launch_ready ? "" : "disabled"}><i data-lucide="play"></i>Launch</button><button class="secondary" type="button" data-configure="${index}"><i data-lucide="settings-2"></i>Configure</button></div></article>`;
    }).join("") : `<div class="empty-state">No games match this search.</div>`;
    $("activity-list").querySelectorAll("[data-configure]").forEach((button) => button.addEventListener("click", () => openEditor(Number(button.dataset.configure))));
    $("activity-list").querySelectorAll("[data-launch]").forEach((button) => button.addEventListener("click", () => launchGame(Number(button.dataset.launch), button)));
    ui.icons($("activity-list"));
  }
  function openEditor(index) {
    selected = index;
    const game = games[index];
    $("activity-empty").hidden = true; $("activity-editor").classList.remove("hidden");
    Object.entries(fields).forEach(([key, id]) => { $(id).value = game[key] ?? ""; });
    $("editor-title").textContent = game.name || "Untitled game";
    $("manifest-path").textContent = manifestPath ? `Manifest: ${manifestPath}` : "";
    document.body.classList.add("drawer-open"); renderList();
  }
  function closeEditor() { document.body.classList.remove("drawer-open"); }
  function updateSelected(key, value) {
    if (!games[selected]) return;
    games[selected][key] = value;
    if (key === "name" || key === "id") renderList();
    if (key === "name") $("editor-title").textContent = value || "Untitled game";
  }
  async function loadGames(showFeedback) {
    try {
      const payload = await ui.api("/api/game/manifest");
      games = (payload.games || []).map(normalize); manifestPath = String(payload.path || ""); renderList();
      const requested = new URLSearchParams(location.search).get("configure");
      if (requested) { const index = games.findIndex((game) => game.id === requested); if (index >= 0) openEditor(index); }
      if (payload.unresolved_count) ui.toast(`${payload.unresolved_count} game path field(s) need attention`, "error");
      else if (showFeedback) ui.toast("Game Library reloaded", "success");
    } catch (error) { $("activity-list").innerHTML = `<div class="notice error">Unable to load games: ${ui.escapeHtml(error.message)}</div>`; }
  }
  async function launchGame(index, button) {
    const game = games[index]; if (!game || !game.launch_ready) return;
    ui.setBusy(button, true, "Launching...");
    try { const payload = await ui.api("/api/game", { method: "POST", body: { action: "launch", name: game.id } }); ui.toast(payload.message || `${game.name || game.id} launched`, "success"); }
    catch (error) { ui.toast(`Unable to launch ${game.name || game.id}: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }
  async function addActivity() {
    games.push(normalize({ players_text: "1-4", recommendation_weight: "0.5", launch_ready: false, path_error: "Executable path is required." }));
    $("activity-search").value = ""; openEditor(games.length - 1); $("activity-id").focus();
  }
  async function deleteActivity() {
    const game = games[selected]; if (!game) return;
    if (!await ui.confirm({ title: "Delete game", message: `Remove ${game.name || game.id || "this game"}? The change is final after saving.`, confirmLabel: "Delete", danger: true })) return;
    games.splice(selected, 1); selected = -1; closeEditor(); renderList(); await saveGames();
  }
  async function browseExecutable() {
    const game = games[selected]; if (!game) return;
    ui.setBusy($("browse-exec"), true, "Opening...");
    try {
      const payload = await ui.api("/api/file/pick", { method: "POST", body: { title: "Select Game Executable", filter: "Executable Files (*.exe)|*.exe|All Files (*.*)|*.*", initial_dir: game.workdir, initial_filename: game.exec } });
      if (!payload.cancelled) { game.exec = String(payload.path || game.exec); game.workdir = String(payload.directory || game.workdir); $("activity-exec").value = game.exec; $("activity-workdir").value = game.workdir; }
    } catch (error) { ui.toast(`File picker failed: ${error.message}`, "error"); }
    finally { ui.setBusy($("browse-exec"), false); }
  }
  async function saveGames() {
    const button = $("save-activities"); ui.setBusy(button, true, "Saving...");
    try {
      const payload = { games: games.map((game) => { const players = parsePlayers(game.players_text); return { id: game.id.trim(), name: game.name.trim(), keywords: splitList(game.keywords_text), description: game.description.trim(), how_to_play: game.how_to_play.trim(), players_min: players.min, players_max: players.max, tags: splitList(game.tags_text), activity_level: game.activity_level.trim(), recommendation_weight: Number(game.recommendation_weight || 0.5) || 0.5, exec: game.exec.trim(), workdir: game.workdir.trim() }; }) };
      const result = await ui.api("/api/game/manifest", { method: "POST", body: payload }); ui.toast(result.message || "Games saved", "success"); await loadGames(false);
    } catch (error) { ui.toast(`Unable to save games: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }
  async function qmdAction(action) {
    const button = $(action === "import" ? "import-qmd" : "export-qmd"); ui.setBusy(button, true, action === "import" ? "Importing..." : "Exporting...");
    try { const payload = await ui.api("/api/qmd", { method: "POST", body: { action } }); ui.toast(payload.message || `QMD ${action} complete`, "success"); if (action === "import") await loadGames(false); }
    catch (error) { ui.toast(`QMD ${action} failed: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }
  Object.entries(fields).forEach(([key, id]) => $(id).addEventListener("input", (event) => updateSelected(key, event.target.value)));
  $("activity-search").addEventListener("input", renderList); $("add-activity").addEventListener("click", addActivity); $("delete-activity").addEventListener("click", deleteActivity); $("browse-exec").addEventListener("click", browseExecutable); $("save-activities").addEventListener("click", saveGames); $("import-qmd").addEventListener("click", () => qmdAction("import")); $("export-qmd").addEventListener("click", () => qmdAction("export")); $("close-editor").addEventListener("click", closeEditor); $("drawer-scrim").addEventListener("click", closeEditor); $("activity-editor").addEventListener("submit", (event) => event.preventDefault());
  $("exit-game").addEventListener("click", async (event) => { const button = event.currentTarget; ui.setBusy(button, true, "Exiting..."); try { const payload = await ui.api("/api/game", { method: "POST", body: { action: "exit", name: "" } }); ui.toast(payload.message || "Exit requested", "success"); } catch (error) { ui.toast(`Exit failed: ${error.message}`, "error"); } finally { ui.setBusy(button, false); } });
  loadGames(false); ui.icons();
}());
