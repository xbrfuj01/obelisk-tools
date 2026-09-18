(function () {
  var STORAGE_KEY = "obelisk-theme";
  var btn = document.getElementById("theme-toggle-btn");
  if (!btn) return;

  var ICONS = {
    system: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/></svg>',
    light: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>',
    dark: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>',
  };
  var LABELS = { system: "Системна", light: "Світла", dark: "Темна" };
  var ORDER = ["system", "light", "dark"];

  function getMode() {
    try {
      var saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "light" || saved === "dark") return saved;
    } catch (err) {}
    return "system";
  }

  function apply(mode) {
    if (mode === "system") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = mode;
    }
    btn.innerHTML = ICONS[mode];
    btn.title = "Тема оформлення: " + LABELS[mode] + " (натисніть, щоб змінити)";
  }

  var mode = getMode();
  apply(mode);

  btn.addEventListener("click", function () {
    mode = ORDER[(ORDER.indexOf(mode) + 1) % ORDER.length];
    try {
      if (mode === "system") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, mode);
    } catch (err) {}
    apply(mode);
  });
})();
