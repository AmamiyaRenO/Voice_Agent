(function () {
  "use strict";
  const dashboardBase = "{{TELEMETRY_DASHBOARD_URL}}";
  const user = document.getElementById("telemetry-user");
  const days = document.getElementById("telemetry-days");
  const frame = document.getElementById("telemetry-frame");
  function url() { return `${dashboardBase}?user_id=${encodeURIComponent(user.value.trim() || "demo_user")}&days=${encodeURIComponent(days.value || "14")}`; }
  function refresh() { frame.src = url(); }
  document.getElementById("refresh-telemetry").addEventListener("click", refresh);
  document.getElementById("open-telemetry").addEventListener("click", () => window.open(url(), "_blank", "noopener"));
  user.addEventListener("change", refresh);
  days.addEventListener("change", refresh);
  refresh();
  window.RachelUI.icons();
}());
