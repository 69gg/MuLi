const THEME_KEY = "muli.theme";
let preference = null;
let systemTheme;

export function resolveTheme(saved, prefersDark = false) {
  return saved === "light" || saved === "dark" ? saved : prefersDark ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", theme);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#14262d" : "#edf4f6");
  document.dispatchEvent(new CustomEvent("muli:themechange", { detail: theme }));
  return theme;
}

export function initializeTheme() {
  try { preference = localStorage.getItem(THEME_KEY); } catch { /* Private browsing may disable storage. */ }
  systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  systemTheme.addEventListener("change", () => { if (!["light", "dark"].includes(preference)) applyTheme(resolveTheme(null, systemTheme.matches)); });
  return applyTheme(resolveTheme(preference, systemTheme.matches));
}

export function toggleTheme() {
  preference = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, preference); } catch { /* Theme still works for this page. */ }
  return applyTheme(preference);
}
