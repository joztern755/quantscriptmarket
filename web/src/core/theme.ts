// Light/dark theme: follows the system by default; a manual choice is stored in localStorage (try/catch).
// public/theme-init.js applies the stored choice before first paint.

export type ThemePref = "light" | "dark" | "system";
const KEY = "aij.theme";
const listeners = new Set<(t: "light" | "dark") => void>();

export function getTheme(): ThemePref {
  try {
    const t = localStorage.getItem(KEY);
    if (t === "light" || t === "dark") return t;
  } catch { /* storage blocked */ }
  return "system";
}

export function effectiveTheme(): "light" | "dark" {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function emit(): void {
  const t = effectiveTheme();
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "dark" ? "#14110E" : "#F6F4F0");
  listeners.forEach((cb) => cb(t));
}

export function setTheme(t: ThemePref): void {
  if (t === "system") {
    document.documentElement.removeAttribute("data-theme");
    try { localStorage.removeItem(KEY); } catch { /* ignore */ }
  } else {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(KEY, t); } catch { /* ignore */ }
  }
  emit();
}

/** Flip between light and dark (stores the explicit choice). */
export function toggleTheme(): void {
  setTheme(effectiveTheme() === "dark" ? "light" : "dark");
}

export function onThemeChange(cb: (t: "light" | "dark") => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function initTheme(): void {
  const t = getTheme();
  if (t !== "system") document.documentElement.setAttribute("data-theme", t);
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    if (getTheme() === "system") emit();
  });
  emit();
}
