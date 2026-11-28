// AeroLink web app: client-side routes `/` (web map), `/device/:id` (pilot screen), `/ops` (ATC dashboard)
// and `/login` (the operators' sign-in, G2).
import "./style.css";

const app = document.querySelector<HTMLDivElement>("#app")!;

async function route(): Promise<void> {
  const m = /^\/device\/([^/]+)\/?$/.exec(location.pathname);
  if (/^\/login\/?$/.test(location.pathname)) {
    const { mount } = await import("./login/index.ts");
    await mount(app);
  } else if (/^\/ops\/?$/.test(location.pathname)) {
    const { mount } = await import("./ops/index.ts");
    await mount(app);
  } else if (/^\/device\/?$/.test(location.pathname)) {
    // No unit in the address: connect by scanning the unit's QR code or by typing its ID and code.
    const { mount } = await import("./pilot/landing.ts");
    mount(app);
  } else if (m) {
    const { mount } = await import("./pilot/index.ts");
    mount(app, decodeURIComponent(m[1]));
  } else {
    const { mount } = await import("./webmap/app.ts");
    await mount(app);
  }
}

void route();
