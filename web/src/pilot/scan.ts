/**
 * QR scanner for the pilot screens: the rear camera in the page, frames decoded in the page with jsQR
 * (BarcodeDetector is not available in iOS Safari). A decoded code is only accepted when `check` says so.
 * Tests drive it through `window.__qrFake` (the next decoded text) and a fake camera stream.
 */
import jsQR from "jsqr";
import { h } from "../shared/dom.ts";

/** null = accept the code; a string = the short reason to show, and keep scanning. */
export type ScanCheck = (url: URL) => string | null;

/** Why the camera could not start, in words a pilot can act on. */
export function cameraMessage(e: unknown): string {
  const name = e instanceof DOMException ? e.name : e instanceof Error ? e.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") return "Camera permission is off. Allow the camera for this site, or enter the unit ID and code.";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "No camera was found on this device. Enter the unit ID and code instead.";
  if (name === "insecure") return "The camera needs a secure (https) connection. Enter the unit ID and code instead.";
  if (name === "unsupported") return "This browser can't open the camera here. Enter the unit ID and code instead.";
  return "Couldn't start the camera. Enter the unit ID and code instead.";
}

/** Start the camera inside `view`. Resolves with a `stop` function; rejects with an Error whose name says why. */
export async function startScanner(view: HTMLElement, check: ScanCheck, onAccept: (url: URL) => void, onMessage: (text: string) => void): Promise<() => void> {
  if (!window.isSecureContext) throw Object.assign(new Error("insecure"), { name: "insecure" });
  if (!navigator.mediaDevices?.getUserMedia) throw Object.assign(new Error("unsupported"), { name: "unsupported" });
  const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
  const video = h("video", { playsinline: true, autoplay: true, class: "scan-video", "aria-label": "Camera preview" });
  video.muted = true;
  video.srcObject = stream;
  view.prepend(video);
  void video.play().catch(() => { /* the preview may need a tap on some browsers; decoding waits for frames */ });
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  let frame = 0;
  let last = 0;
  let done = false;
  const stop = (): void => {
    if (done) return;
    done = true;
    cancelAnimationFrame(frame);
    document.removeEventListener("visibilitychange", onVisibility);
    stream.getTracks().forEach((t) => t.stop());
    video.srcObject = null;
    video.remove();
  };
  // The camera is released when the page is hidden (the pilot switches apps) or when the caller stops it.
  const onVisibility = (): void => { if (document.hidden) stop(); };
  document.addEventListener("visibilitychange", onVisibility);

  const tick = (now: number): void => {
    if (done) return;
    if (now - last >= 120) { // ~8 decodes a second is plenty and keeps the phone cool
      last = now;
      const text = nextText(ctx, canvas, video);
      if (text !== null) {
        let url: URL | null = null;
        try { url = new URL(text); } catch { url = null; }
        const why = url ? check(url) : "That isn't an AeroLink code.";
        if (url && why === null) { stop(); onAccept(url); return; }
        onMessage(why ?? "That isn't an AeroLink code.");
      }
    }
    frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
  return stop;
}

/** The decoded text of the current frame, or null. The test seam wins when a test has set one. */
function nextText(ctx: CanvasRenderingContext2D | null, canvas: HTMLCanvasElement, video: HTMLVideoElement): string | null {
  const fake = (window as unknown as { __qrFake?: string | null }).__qrFake;
  if (fake) {
    (window as unknown as { __qrFake?: string | null }).__qrFake = null;
    return fake;
  }
  if (!ctx || video.readyState < 2 || !video.videoWidth) return null;
  const scale = Math.min(1, 640 / video.videoWidth);
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
  return jsQR(img.data, img.width, img.height)?.data ?? null;
}

/** Accept a pairing link for this site (and, when `unit` is given, for that unit only). Returns the reason otherwise. */
export function checkUnitUrl(url: URL, unit?: string): string | null {
  if (url.origin !== location.origin) return "That code is for another site.";
  const m = /^\/device\/([^/]+)\/?$/.exec(url.pathname);
  if (!m) return "That code isn't for an AeroLink unit.";
  if (unit && decodeURIComponent(m[1]) !== unit) return "That code is for another unit.";
  if (!url.searchParams.get("code")) return "That code has no pairing code in it.";
  return null;
}
