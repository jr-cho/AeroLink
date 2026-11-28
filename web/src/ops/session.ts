/** The operator's login session (G2): who is signed in, signing out, and leaving for the login page when it ends. */
import { ApiError } from "../shared/api.ts";

export interface Operator {
  username: string;
  name: string;
  /** ISO time the server ends this session (12 h after login). */
  expires_at: string;
  /** The AI disclaimer version this operator last acknowledged (contract §3); null: not yet. */
  ai_ack_version?: string | null;
}

/** `null`: not signed in. `undefined`: the server could not be reached (the page then just shows its offline pill). */
export async function whoami(): Promise<Operator | null | undefined> {
  try {
    const r = await fetch("/api/auth/me");
    if (r.status === 401) return null;
    if (!r.ok) return undefined;
    return (await r.json()) as Operator;
  } catch {
    return undefined;
  }
}

export const isUnauthorized = (e: unknown): boolean => e instanceof ApiError && e.status === 401;

let leaving = false;
/** Go to the login page; it sends the operator back here afterwards. `expired` adds a short notice there. */
export function toLogin(expired: boolean): void {
  if (leaving) return;
  leaving = true;
  const next = encodeURIComponent(location.pathname + location.search);
  location.assign(`/login?next=${next}${expired ? "&expired=1" : ""}`);
}

export async function signOut(): Promise<void> {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch {
    /* the cookie is dropped by the server on the next successful call; leave anyway */
  }
  toLogin(false);
}

/**
 * Leave a few seconds before the server ends the session. Without this the browser would already have dropped the
 * cookie and the next poll would meet Caddy's basic-auth prompt instead of a quiet redirect.
 */
export function watchExpiry(expiresAt: string, marginMs = 5000): void {
  const left = Date.parse(expiresAt) - Date.now() - marginMs;
  if (Number.isFinite(left)) setTimeout(() => toLogin(true), Math.max(0, left));
}
