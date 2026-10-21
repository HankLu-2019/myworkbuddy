/** System / Light / Dark (07 §4.12 Profile). Stored per browser; applied before the first render. */
export type Theme = "system" | "light" | "dark";
const KEY = "mwb:theme";

export function readTheme(): Theme {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function saveTheme(theme: Theme) {
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch {
    // not kept; still applied for this page
  }
  applyTheme(theme);
}
