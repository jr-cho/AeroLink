/** Pairing: scan the unit's QR (`/device/<id>?code=...`) or type the code printed on its label. */
import { clear, h } from "../shared/dom.ts";
import { PilotError, pilotApi } from "./net.ts";
import { cameraMessage, checkUnitUrl, startScanner } from "./scan.ts";

export type Entry = "paired" | "need_code" | "unknown";

function stripCodeFromUrl(): void {
  const u = new URL(location.href);
  if (u.searchParams.has("code")) {
    u.searchParams.delete("code");
    history.replaceState(history.state, "", u.pathname + (u.search ? u.search : "") + u.hash);
  }
}

/** Decide how the page starts: pair with `?code`, or reuse the session cookie, or ask for a code. */
export async function enter(deviceId: string): Promise<{ entry: Entry; message?: string }> {
  const code = new URL(location.href).searchParams.get("code");
  let message: string | undefined;
  if (code) {
    try {
      await pilotApi.pair(deviceId, code);
      stripCodeFromUrl();
      return { entry: "paired" };
    } catch (e) {
      message = pairMessage(e);
      stripCodeFromUrl(); // a code never stays in the address bar
    }
  }
  try {
    await pilotApi.state(deviceId);
    return { entry: "paired" };
  } catch (e) {
    if (e instanceof PilotError && e.status === 404) return { entry: "unknown" };
    return { entry: "need_code", message };
  }
}

export function pairMessage(e: unknown): string {
  if (e instanceof PilotError) {
    if (e.status === 429) return "Too many attempts. Wait a minute and try again.";
    if (e.status === 403) return "That code didn't work. Check the code on the unit's label.";
    return e.message;
  }
  return "Can't reach AeroLink. Check your connection.";
}

/** Case, spaces and dashes don't matter; show it tidily as the user types. */
export function normalizeCode(raw: string): string {
  return raw.replace(/[^a-zA-Z0-9]/g, "").toUpperCase();
}

export function showPairScreen(root: HTMLElement, deviceId: string, message: string | undefined, onPaired: () => void): void {
  clear(root);
  root.className = "pilot";
  const err = h("p", { class: "pair-error", id: "pair-error", role: "alert" }, message ?? "");
  const input = h("input", { id: "pair-code", class: "pair-input", autocomplete: "off", autocapitalize: "characters", spellcheck: "false", inputmode: "text", placeholder: "XXXX-XXXXXX", "aria-label": "Pairing code" });
  const btn = h("button", { id: "pair-go", class: "btn primary big", type: "button" }, "Pair");
  async function submit(): Promise<void> {
    const code = normalizeCode(input.value);
    if (!code) return;
    btn.disabled = true;
    err.textContent = "";
    try {
      await pilotApi.pair(deviceId, code);
      onPaired();
    } catch (e) {
      err.textContent = pairMessage(e);
      btn.disabled = false;
    }
  }
  btn.addEventListener("click", () => void submit());
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") void submit(); });
  // Scan in the page (this unit's code only); the camera is released on success, close, or when the page is hidden.
  let stopCamera: (() => void) | null = null;
  const scanArea = h("section", { class: "scan-area", id: "pair-scan-area", hidden: true, "aria-label": "Camera" }, h("div", { class: "scan-frame", "aria-hidden": "true" }));
  const scanStatus = h("p", { id: "pair-scan-status", class: "muted", role: "status" });
  const scanClose = h("button", { id: "pair-scan-close", class: "btn big", type: "button" }, "Close camera");
  const stopScan = (): void => { stopCamera?.(); stopCamera = null; scanArea.hidden = true; scanBtn.hidden = false; };
  const scanBtn = h("button", { id: "pair-scan", class: "btn primary big", type: "button" }, "Scan QR code");
  scanBtn.addEventListener("click", () => {
    scanBtn.hidden = true;
    scanArea.hidden = false;
    scanStatus.textContent = "Point the camera at the QR code on your unit.";
    scanStatus.className = "muted";
    startScanner(scanArea, (u) => checkUnitUrl(u, deviceId), (u) => { stopScan(); location.href = u.href; }, (msg) => { scanStatus.textContent = msg; scanStatus.className = "pair-error"; })
      .then((stop) => { stopCamera = stop; })
      .catch((e) => { stopScan(); err.textContent = cameraMessage(e); });
  });
  scanClose.addEventListener("click", stopScan);
  scanArea.append(scanStatus, scanClose);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopScan(); });
  root.append(h("main", { class: "pair-screen" },
    h("div", { class: "pair-logo" }, "AeroLink"),
    h("h1", {}, "Scan the unit's QR code"),
    h("p", { class: "muted" }, "Scan the QR code on your AeroLink unit with this page or your phone's Camera app, or type the pairing code."),
    scanBtn, scanArea, h("p", { class: "muted" }, "Or type the pairing code from the unit's label:"),
    input, btn, err));
}

export function showUnknown(root: HTMLElement, deviceId: string): void {
  clear(root);
  root.className = "pilot";
  root.append(h("main", { class: "pair-screen" }, h("h1", {}, "Unknown unit"), h("p", { class: "muted" }, `${deviceId} isn't registered. Scan the QR code on your unit.`)));
}
