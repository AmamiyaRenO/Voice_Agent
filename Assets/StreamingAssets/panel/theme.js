(function () {
  "use strict";
  const storageKey = "rachel-console-theme";
  let theme = "dark";
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (error) { }
  document.documentElement.dataset.theme = theme;
  window.RachelTheme = {
    get: () => document.documentElement.dataset.theme || "dark",
    set: (value) => {
      const next = value === "light" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { window.localStorage.setItem(storageKey, next); } catch (error) { }
      window.dispatchEvent(new CustomEvent("rachel:theme", { detail: { theme: next } }));
      return next;
    }
  };
}());
