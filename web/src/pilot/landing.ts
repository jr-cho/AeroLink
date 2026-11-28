/** `/device` with no unit: connect by scanning the unit's QR code, or by typing its unit ID and pairing code. */
import { clear, h } from "../shared/dom.ts";
import { cameraMessage, checkUnitUrl, startScanner } from "./scan.ts";
import "./pilot.css";

/** Night from 19:00 to 06:00, as on the pilot screen. */
function nightNow(): boolean {
  const hr = new Date().getHours();
  return hr >= 19 || hr < 6;
}

export function mount(root: HTMLElement): void {
  clear(root);
  root.className = `pilot ${nightNow() ? "night" : "day"}`;
  let stopCamera: (() => void) | null = null;
  const scanGo = h("button", { id: "scan-go", class: "btn primary big", type: "button" }, "Scan QR code");
  const scanStatus = h("p", { id: "scan-status", class: "muted", role: "status" });
  const scanArea = h("section", { id: "scan-area", class: "scan-area", hidden: true, "aria-label": "Camera" },
    h("div", { class: "scan-frame", "aria-hidden": "true" }),
    scanStatus,
    h("button", { id: "scan-close", class: "btn big", type: "button" }, "Close camera"));
  const fallback = h("p", { id: "scan-fallback", class: "pair-error", role: "alert" });
  const manualToggle = h("button", { id: "manual-toggle", class: "btn big", type: "button" }, "Enter unit ID and code");
  const unitIn = h("input", { id: "manual-unit", class: "pair-input manual-input", autocomplete: "off", autocapitalize: "characters", spellcheck: "false", placeholder: "Unit ID", "aria-label": "Unit ID" });
  const codeIn = h("input", { id: "manual-code", class: "pair-input manual-input", autocomplete: "off", autocapitalize: "characters", spellcheck: "false", placeholder: "Pairing code", "aria-label": "Pairing code" });
  const manualMsg = h("p", { id: "manual-msg", class: "pair-error", role: "alert" });
  const manual = h("form", { id: "manual-form", class: "manual", hidden: true },
    unitIn, codeIn, manualMsg,
    h("button", { id: "manual-go", class: "btn primary big", type: "submit" }, "Connect"));
  const stop = (): void => { stopCamera?.(); stopCamera = null; intro.hidden = false; };
  const intro = h("p", { class: "muted", id: "connect-intro" }, "Scan the QR code on your unit with this page or your phone's Camera app, or type the unit ID and pairing code.");

  scanGo.addEventListener("click", () => void openCamera());
  async function openCamera(): Promise<void> {
    fallback.textContent = "";
    scanStatus.textContent = "Point the camera at the QR code on your unit.";
    scanStatus.className = "muted";
    scanGo.hidden = true;
    scanArea.hidden = false;
    intro.hidden = true; // the camera view needs the room
    try {
      stopCamera = await startScanner(scanArea, (u) => checkUnitUrl(u), (u) => { stop(); location.href = u.href; }, (msg) => { scanStatus.textContent = msg; scanStatus.className = "pair-error"; });
    } catch (e) {
      scanArea.hidden = true;
      scanGo.hidden = false;
      intro.hidden = false;
      fallback.textContent = cameraMessage(e);
      manual.hidden = false;
      manualToggle.hidden = true;
      unitIn.focus();
    }
  }
  scanArea.querySelector("#scan-close")!.addEventListener("click", () => {
    stop();
    scanArea.hidden = true;
    scanGo.hidden = false;
  });
  // The camera is released when the page is hidden (scanner also listens for this itself).
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });

  manualToggle.addEventListener("click", () => { manual.hidden = false; manualToggle.hidden = true; unitIn.focus(); });
  manual.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const unit = unitIn.value.trim();
    const code = codeIn.value.trim();
    if (!unit || !code) { manualMsg.textContent = "Enter both the unit ID and the pairing code."; return; }
    location.href = `/device/${encodeURIComponent(unit)}?code=${encodeURIComponent(code)}`;
  });

  root.append(h("main", { class: "pair-screen connect-screen" },
    h("div", { class: "pair-logo" }, "AeroLink"),
    h("h1", {}, "Connect to your AeroLink unit"),
    intro, scanGo, scanArea, fallback, manualToggle, manual));
}
