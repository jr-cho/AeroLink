/** The pilot screen (`/device/<id>`, ARCHITECTURE §13.2). Contract: `mount(el, deviceId)`. */
import { enter, showPairScreen, showUnknown } from "./pair.ts";
import "./pilot.css";

export async function mount(el: HTMLElement, deviceId: string): Promise<void> {
  async function start(): Promise<void> {
    const { startApp } = await import("./app.ts");
    await startApp(el, deviceId, () => void boot("Your session ended. Pair again."));
  }
  async function boot(message?: string): Promise<void> {
    const r = await enter(deviceId);
    if (r.entry === "paired") return start();
    if (r.entry === "unknown") return showUnknown(el, deviceId);
    showPairScreen(el, deviceId, message ?? r.message, () => void start());
  }
  el.className = "pilot";
  await boot();
}
