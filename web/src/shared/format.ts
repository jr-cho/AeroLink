/** Small formatting helpers. */

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m < 10 && r >= 10 ? `${m} min ${r} s` : `${m} min`;
}

/** Aviation units (user decision 2026-11-26): distance in nautical miles, speed in knots, altitude in feet.
 *  The backend stays SI (metres, m/s); every screen formats through these. */
export const M_PER_NM = 1852;
export function fmtNm(m: number): string {
  const nm = m / M_PER_NM;
  return `${nm < 10 ? nm.toFixed(1) : Math.round(nm)} NM`;
}
export function fmtKt(mps: number): string {
  return `${Math.round(mps * 1.943844)} kt`;
}
export function fmtFt(m: number): string {
  return `${Math.round((m * 3.28084) / 10) * 10} ft`;
}
/** Route distances (kept for existing callers): nautical miles. */
export function fmtDistance(m: number): string {
  return fmtNm(m);
}

/** Local clock time from an ISO string or epoch seconds. */
export function fmtClock(t: string | number): string {
  const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function toEpoch(t: string | number): number {
  return typeof t === "number" ? t : new Date(t).getTime() / 1000;
}

export function fmtPct(x: number): string {
  return `${Math.round(x * 100)}%`;
}
