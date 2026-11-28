/**
 * Appearance of AeroLink Ops and its login page (E6.14): Light (default), Dark, or Auto (day by local time).
 * Remembered per browser. The same CarPlay-style palettes as the pilot screen; no STARS look any more.
 */
import type { Theme } from "../shared/mapStyles/carplay.ts";

export type Appearance = "light" | "dark" | "auto";
const KEY = "aerolink.ops.appearance";

export function getAppearance(): Appearance {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "auto") return v;
  } catch {
    /* storage blocked: the default */
  }
  return "light";
}

export function saveAppearance(a: Appearance): void {
  try {
    localStorage.setItem(KEY, a);
  } catch {
    /* not remembered; still applied */
  }
}

/** Auto: day from 06:00 to 18:59 local time. */
export function resolveTheme(a: Appearance, now = new Date()): Theme {
  if (a === "light") return "day";
  if (a === "dark") return "night";
  const hr = now.getHours();
  return hr >= 6 && hr < 19 ? "day" : "night";
}
