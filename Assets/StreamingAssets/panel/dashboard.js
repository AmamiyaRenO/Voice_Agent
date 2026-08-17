(function () {
  "use strict";
  const ui = window.RachelUI;
  const grid = document.getElementById("application-grid");

  function playerLabel(game) {
    const min = Number(game.players_min || 0);
    const max = Number(game.players_max || 0);
    if (min && max) return min === max ? `${min} player${min === 1 ? "" : "s"}` : `${min}-${max} players`;
    return "Players not set";
  }

  function playerIcon(game) {
    const min = Number(game.players_min || 0);
    const max = Number(game.players_max || 0);
    return min === 1 && max === 1 ? "user-round" : "users";
  }

  function gameIcon(game) {
    const value = `${game.id || ""} ${(game.tags || []).join(" ")}`.toLowerCase();
    if (value.includes("disc") || value.includes("golf")) return "disc-3";
    if (value.includes("music") || value.includes("dance")) return "music-2";
    if (value.includes("story")) return "book-open";
    return "gamepad-2";
  }

  async function launch(game, button) {
    ui.setBusy(button, true, "Launching...");
    try {
      const payload = await ui.api("/api/game", { method: "POST", body: { action: "launch", name: game.id } });
      ui.toast(payload.message || `${game.name || game.id} launched`, "success");
    } catch (error) { ui.toast(`Unable to launch ${game.name || game.id}: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  }

  function render(games) {
    if (!games.length) {
      grid.innerHTML = `<div class="empty-state"><div><i data-lucide="library"></i><h2>No games configured</h2><p class="panel-note">Add the first game in Game Library.</p><a class="button" href="/games.html">Open Game Library</a></div></div>`;
      ui.icons(grid); return;
    }
    grid.innerHTML = games.map((game) => {
      const ready = game.launch_ready !== false && !game.path_error;
      return `<article class="application-card">
        <div class="application-card-head"><span class="application-icon"><i data-lucide="${gameIcon(game)}"></i></span><span class="status-badge ${ready ? "ok" : "error"}">${ready ? "Ready" : "Path needed"}</span></div>
        <div class="application-copy"><h2>${ui.escapeHtml(game.name || game.id)}</h2><p>${ui.escapeHtml(game.description || game.how_to_play || "No description has been added.")}</p><div class="application-meta"><span class="chip"><i data-lucide="${playerIcon(game)}"></i>${ui.escapeHtml(playerLabel(game))}</span><span class="chip"><i data-lucide="activity"></i>${ui.escapeHtml(game.activity_level || "Not set")}</span></div>${game.path_error ? `<p class="notice error">${ui.escapeHtml(game.path_error)}</p>` : ""}</div>
        <div class="application-actions">${ready ? `<button class="primary" type="button" data-launch="${ui.escapeHtml(game.id)}"><i data-lucide="play"></i>Launch</button>` : `<a class="button" href="/games.html?configure=${encodeURIComponent(game.id)}"><i data-lucide="wrench"></i>Fix path</a>`}<a class="button secondary" href="/games.html?configure=${encodeURIComponent(game.id)}" aria-label="Configure ${ui.escapeHtml(game.name || game.id)}"><i data-lucide="settings-2"></i>Configure</a></div>
      </article>`;
    }).join("");
    grid.querySelectorAll("[data-launch]").forEach((button) => {
      const game = games.find((item) => String(item.id) === button.dataset.launch);
      button.addEventListener("click", () => launch(game, button));
    });
    ui.icons(grid);
  }

  async function load() {
    try {
      const payload = await ui.api("/api/game/manifest");
      render(Array.isArray(payload.games) ? payload.games : []);
    } catch (error) {
      grid.innerHTML = `<div class="notice error">Unable to load applications: ${ui.escapeHtml(error.message)}</div>`;
    }
  }

  document.getElementById("exit-game").addEventListener("click", async (event) => {
    const button = event.currentTarget; ui.setBusy(button, true, "Exiting...");
    try { const payload = await ui.api("/api/game", { method: "POST", body: { action: "exit", name: "" } }); ui.toast(payload.message || "Exit requested", "success"); }
    catch (error) { ui.toast(`Unable to exit game: ${error.message}`, "error"); }
    finally { ui.setBusy(button, false); }
  });
  load();
}());
