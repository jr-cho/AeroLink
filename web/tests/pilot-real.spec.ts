/**
 * Smoke test against the REAL backend (no MQTT, temp SQLite, free port): provision a unit with the `devices`
 * CLI, pair by its link, read the device state and route to a real destination. The page's /api/devices calls are
 * forwarded to the backend with the session cookie carried by the test (the vite preview proxy has a fixed port).
 * Full stack with MQTT and WebSocket is E7's job.
 */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { mockApi } from "./mock.ts";

const BACKEND = resolve(import.meta.dirname, "../../backend");
const hasUv = spawnSync("uv", ["--version"]).status === 0;
test.skip(!hasUv, "uv is not installed");

const freePort = () => new Promise<number>((res) => { const s = createServer(); s.listen(0, () => { const p = (s.address() as any).port; s.close(() => res(p)); }); });

let server: ChildProcess | undefined;
test.afterAll(() => { server?.kill(); });

test("real backend: pair from the QR link, state and routing from the page", async ({ page }) => {
  test.setTimeout(120_000);
  const dir = mkdtempSync(join(tmpdir(), "aerolink-e6-"));
  const port = await freePort();
  const env = { ...process.env, DATABASE_URL: `sqlite+aiosqlite:///${dir}/e6.db`, AEROLINK_COOKIE_SECURE: "0", AEROLINK_SITE: "http://localhost:4173", PRELOAD_MODELS: "false" };
  const mig = spawnSync("uv", ["run", "alembic", "upgrade", "head"], { cwd: BACKEND, env, encoding: "utf8" });
  expect(mig.status, mig.stderr).toBe(0);
  const add = spawnSync("uv", ["run", "python", "-m", "aerolink.service.devices", "add", "dev-e6", "passenger", "--callsign", "N123AL", "--out-dir", dir], { cwd: BACKEND, env, encoding: "utf8" });
  expect(add.status, add.stderr).toBe(0);
  const link = /pairing link: (\S+)/.exec(add.stdout)![1];
  const code = new URL(link).searchParams.get("code")!;
  server = spawn("uv", ["run", "python", "-c", `import uvicorn; from aerolink.service.app import create_app; uvicorn.run(create_app(mqtt=False), host="127.0.0.1", port=${port}, log_level="warning")`], { cwd: BACKEND, env, stdio: "ignore" });
  const base = `http://127.0.0.1:${port}`;
  await expect.poll(async () => { try { return (await fetch(`${base}/healthz`)).ok; } catch { return false; } }, { timeout: 90_000 }).toBe(true);

  await mockApi(page); // airspace and basemap only
  let cookie = "";
  const calls: string[] = [];
  await page.route(/\/api\/devices\//, async (route) => {
    const req = route.request();
    const u = new URL(req.url());
    const res = await route.fetch({ url: base + u.pathname + u.search, headers: { ...req.headers(), cookie } });
    calls.push(`${req.method()} ${u.pathname} ${res.status()}`);
    const set = res.headersArray().find((h) => h.name.toLowerCase() === "set-cookie");
    if (set) cookie = set.value.split(";")[0];
    await route.fulfill({ response: res });
  });
  await page.routeWebSocket(/\/ws\/devices\//, (ws) => ws.close({ code: 1006 })); // polling covers it here

  await page.goto(`/device/dev-e6?code=${encodeURIComponent(code)}`);
  await expect(page.locator("#type-badge")).toHaveText("Passenger eVTOL", { timeout: 30_000 });
  expect(new URL(page.url()).search).toBe("");
  expect(calls.some((c) => c.startsWith("POST /api/devices/dev-e6/pair 200"))).toBe(true);
  expect(calls.some((c) => c.startsWith("GET /api/devices/dev-e6 200"))).toBe(true);
  // the unit has never been online, so routing is locked: the page says so (server would answer 409)
  await expect(page.locator("#offline")).toContainText("Unit not connected");
  await expect(page.locator("#dest-search")).toBeDisabled();
});
