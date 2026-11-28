/**
 * The operator login (`/login`, G2): one card in the dashboard's CarPlay look (light by default, dark per the appearance setting). Pilots never see it
 * (they pair a screen by scanning the unit's QR code). Contract: `mount(el)`.
 *
 * `?next=/ops` is where to go afterwards (a path on this site only: never an address someone else could supply);
 * `?expired=1` adds a short notice that the earlier session ended.
 */
import { clear, h } from "../shared/dom.ts";
import { getAppearance, resolveTheme } from "../ops/theme.ts";
import "./login.css";

/** A same-site path, else the dashboard. Rejects `//host`, `/\host` and anything with a scheme. */
export function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\") || /[\r\n]/.test(raw)) return "/ops";
  if (raw.startsWith("/login")) return "/ops"; // never bounce back to this page
  return raw;
}

const MESSAGES: Record<number, string> = {
  401: "Wrong username or password.",
  429: "Too many attempts. Wait a minute and try again.",
};

export async function mount(root: HTMLElement): Promise<void> {
  document.title = "Sign in to AeroLink Ops";
  const params = new URLSearchParams(location.search);
  const next = safeNext(params.get("next"));
  // already signed in (a bookmark to /login): straight on
  try {
    if ((await fetch("/api/auth/me")).ok && !params.has("expired")) return location.replace(next);
  } catch { /* the form will say the server is unreachable */ }

  clear(root);
  root.className = `login ${resolveTheme(getAppearance()) === "night" ? "night" : ""}`;
  const user = h("input", { id: "username", name: "username", type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: false, required: true, autofocus: true });
  const pass = h("input", { id: "password", name: "password", type: "password", autocomplete: "current-password", required: true });
  const error = h("p", { class: "error", id: "login-error", role: "alert", "aria-live": "assertive" });
  const button = h("button", { type: "submit", id: "signin", class: "primary" }, "Sign in");
  const form = h("form", { id: "login-form", novalidate: true },
    h("h1", {}, "AeroLink Ops sign-in"),
    params.has("expired") ? h("p", { class: "notice", id: "login-notice" }, "Your session ended. Sign in again.") : null,
    h("label", { for: "username" }, "Username"), user,
    h("label", { for: "password" }, "Password"), pass,
    error, button);

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    if (!user.value.trim() || !pass.value) { error.textContent = "Enter your username and password."; return; }
    button.disabled = true;
    try {
      const r = await fetch("/api/auth/login", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ username: user.value.trim(), password: pass.value }) });
      if (r.ok) return location.assign(next);
      error.textContent = MESSAGES[r.status] ?? "Could not sign in. Try again.";
      pass.value = "";
      pass.focus();
    } catch {
      error.textContent = "Cannot reach the server.";
    } finally {
      button.disabled = false;
    }
  });
  root.append(h("main", { class: "card" }, form));
  user.focus();
}
