/** The ATC operations dashboard (`/ops`, ARCHITECTURE §13.4). Contract: `mount(el)`. Needs an operator login (G2). */
import { toLogin, whoami } from "./session.ts";

export async function mount(el: HTMLElement): Promise<void> {
  document.title = "AeroLink Ops";
  const who = await whoami();
  if (who === null) return toLogin(false); // no session: the login page, then straight back here
  const { mount: start } = await import("./app.ts");
  await start(el, who ?? null);
}
