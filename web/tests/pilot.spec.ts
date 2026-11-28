import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { AI_INFO, CODE, DEVICE, DISCLAIMER_VERSION, PILOT_DISCLAIMER, makeReroute, mockPilot, type PilotMock } from "./pilot-mock.ts";
import { findPath, nodes } from "./mock.ts";

const SHOT = "test-results/screenshots";
/** Screenshot-name suffix: the portrait project keeps the plain names, the landscape ones add their project name. */
let proj = "";
test.beforeEach(({}, ti) => { proj = ti.project.name === "pilot" ? "" : `-${ti.project.name}`; });
const URL_QR = `/device/${DEVICE}?code=${CODE}`;

async function openHome(page: Page, init: Partial<PilotMock> = {}, url = URL_QR) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const m = await mockPilot(page, init);
  await page.goto(url);
  await expect(page.locator("#type-badge")).toBeVisible();
  await expect(page.locator("#link")).toHaveText("Live");
  return { m, errors };
}

async function requestRoute(page: Page, name: string) {
  await page.click("#dest-search");
  await page.fill("#dest-search", name);
  await page.locator(".dest-row").first().click();
  await expect(page.locator(".route-card").first()).toBeVisible();
}

test("pairs via the QR link, strips the code from the URL, shows the unit's type", async ({ page }) => {
  const { m, errors } = await openHome(page);
  expect(m.pairCalls).toEqual([{ code: CODE }]);
  expect(new URL(page.url()).search).toBe("");
  await expect(page.locator("#type-badge")).toHaveText("Passenger eVTOL");
  await expect(page.locator("#dest-search")).toBeEnabled();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${SHOT}/pilot-01-home${proj}.png` });
  // reload with the session: no code needed, no second pairing
  await page.goto(`/device/${DEVICE}`);
  await expect(page.locator("#type-badge")).toBeVisible();
  expect(m.pairCalls.length).toBe(1);
  expect(errors).toEqual([]);
});

test("no code and no session shows the scan screen; typing the code (any case, spaces) pairs", async ({ page }) => {
  const m = await mockPilot(page);
  await page.goto(`/device/${DEVICE}`);
  await expect(page.locator("h1")).toHaveText("Scan the unit's QR code");
  await page.fill("#pair-code", "wrong 000");
  await page.click("#pair-go");
  await expect(page.locator("#pair-error")).toContainText("didn't work");
  await page.screenshot({ path: `${SHOT}/pilot-02-scan${proj}.png` });
  await page.fill("#pair-code", "k7qf 2xm4tb");
  await page.click("#pair-go");
  await expect(page.locator("#type-badge")).toBeVisible();
  expect(m.paired).toBe(true);
});

test("a wrong code in the QR link shows the scan screen and leaves no code in the URL", async ({ page }) => {
  await mockPilot(page);
  await page.goto(`/device/${DEVICE}?code=NOPE-NOPE00`);
  await expect(page.locator("#pair-error")).toContainText("didn't work");
  expect(new URL(page.url()).search).toBe("");
});

test("full flow: Where to? -> options -> Go -> planned -> active -> faster route -> Accept -> ended", async ({ page }) => {
  const { m } = await openHome(page);
  await page.click("#dest-search");
  const names = await page.locator(".dest-name").allTextContents();
  expect(names.length).toBeGreaterThan(2);
  const dists = await page.locator(".dest-row .muted").allTextContents();
  expect(dists[0]).toMatch(/\d/);
  await page.screenshot({ path: `${SHOT}/pilot-03-where${proj}.png` });
  await page.fill("#dest-search", "SunTrax");
  await page.locator(".dest-row").first().click();
  await expect(page.locator(".route-card")).toHaveCount(2);
  const req = m.requests.find((r) => r.path.endsWith("/route-request"))!;
  expect(Object.keys(req.body).sort()).toEqual(["destination", "request_id"]); // no vehicle type from the screen
  const card = page.locator(".route-card").first();
  await expect(card).toContainText("Recommended");
  await expect(card).toContainText("Fastest");
  await expect(card).toContainText("Arrive");
  await expect(card).toContainText("traffic");
  await expect(card).toContainText("Shortest, light traffic");
  const colours = await page.evaluate(() => (window as any).__map.getSource("routes").serialize().data.features.map((f: any) => f.properties.color));
  expect(new Set(colours).size).toBe(2);
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${SHOT}/pilot-04-options${proj}.png` });

  await card.locator("[data-go]").click();
  await expect(page.locator("#flight-chip")).toHaveText("Planned");
  const sel = m.requests.find((r) => r.path.endsWith("/route-selection"))!;
  expect(Object.keys(sel.body).sort()).toEqual(["request_id", "route_id"]);
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "active", reason: null, timestamp: new Date().toISOString() });
  await expect(page.locator("#flight-chip")).toHaveText("Active");
  await expect(page.locator("#next-name")).not.toHaveText("None");
  await expect(page.locator("#next-eta")).toContainText("ETA");
  m.push("traffic", { aircraft_id: DEVICE, forecast_time: new Date().toISOString(), aircraft: [{ id: "AL-02", vehicle_type: "passenger", lat: 28.4, lon: -81.4, alt_msl_m: 900, course_deg: 10, off_route: false }], edges: m.flight.path.slice(1).map((n: string, i: number) => ({ edge: `${m.flight.path[i]}->${n}`, now: "heavy", in_15_min: "heavy", in_30_min: "low" })) });
  await expect(page.locator("#traffic-ahead")).toContainText("Heavy");
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/pilot-05-navigate${proj}.png` });

  // a revised route-response for the running request: a new, quicker route
  const better = m.routes(m.dest!, sel.body.request_id, 1, 5);
  better.routes = [{ ...better.routes[0], est_time_s: 420 }];
  m.push("route-response", better);
  await expect(page.locator("#faster")).toContainText("Faster route available, saves");
  await expect(page.locator("#faster")).toContainText("min");
  await page.screenshot({ path: `${SHOT}/pilot-06-faster${proj}.png` });
  await page.click("#accept");
  await expect.poll(() => m.requests.filter((r) => r.path.endsWith("/route-selection")).length).toBe(2);
  const sel2 = m.requests.filter((r) => r.path.endsWith("/route-selection"))[1];
  expect(sel2.body).toEqual({ request_id: sel.body.request_id, route_id: better.routes[0].route_id });
  await expect(page.locator("#faster")).toHaveCount(0);

  m.flight.status = "ended";
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "ended", reason: null, timestamp: new Date().toISOString() });
  await expect(page.locator("#arrived")).toContainText("Flight ended");
  await page.click("#done");
  await expect(page.locator("#dest-search")).toBeEnabled();
});

test("Ignore dismisses the banner; off-route shows 'Off route. Recalculating'", async ({ page }) => {
  const { m } = await openHome(page);
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  const sel = m.requests.find((r) => r.path.endsWith("/route-selection"))!;
  const better = m.routes(m.dest!, sel.body.request_id, 1, 6);
  m.push("route-response", better);
  await expect(page.locator("#faster")).toBeVisible();
  await page.click("#ignore");
  await expect(page.locator("#faster")).toHaveCount(0);
  m.flight.status = "off_route"; // the server's state agrees with the push (as flyToSunTrax does for "active")
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "off_route", reason: "off", timestamp: new Date().toISOString() });
  await expect(page.locator("#off-route")).toHaveText("Off route. Recalculating");
  await expect(page.locator("#flight-chip")).toHaveText("Off route");
});

test("a state poll asked before a pushed flight-status does not overwrite it", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m); // the server says "active"
  let held: import("@playwright/test").Route | null = null;
  await page.route(new RegExp(`/api/devices/${DEVICE}$`), (route) => {
    if (route.request().method() === "GET" && !held) held = route; // hold the next state poll
    else void route.fallback();
  });
  await expect.poll(() => held !== null, { timeout: 15_000 }).toBe(true);
  // the push arrives while that older poll is still on its way; the poll still says "active"
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "off_route", reason: "off", timestamp: new Date().toISOString() });
  await expect(page.locator("#flight-chip")).toHaveText("Off route");
  await (held as unknown as import("@playwright/test").Route).fallback();
  await page.waitForTimeout(1200);
  await expect(page.locator("#flight-chip")).toHaveText("Off route");
  await expect(page.locator("#off-route")).toBeVisible();
});

test("wake lock is requested while navigating and released after", async ({ page }) => {
  await page.addInitScript(() => {
    const w = window as any;
    w.__wake = { requests: 0, releases: 0 };
    Object.defineProperty(navigator, "wakeLock", { value: { request: async () => { w.__wake.requests++; return { release: async () => { w.__wake.releases++; }, addEventListener() {} }; } } });
  });
  const { m } = await openHome(page);
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  await expect.poll(() => page.evaluate(() => (window as any).__wake.requests)).toBeGreaterThan(0);
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "ended", reason: null, timestamp: new Date().toISOString() });
  await expect.poll(() => page.evaluate(() => (window as any).__wake.releases)).toBeGreaterThan(0);
});

test("offline: 'Unit not connected' within 5 s, routing disabled and not called; back online restores", async ({ page }) => {
  const { m } = await openHome(page);
  const before = m.requests.filter((r) => r.method === "POST").length;
  m.online = false;
  m.push("offline", { at: new Date().toISOString() });
  await expect(page.locator("#offline")).toBeVisible({ timeout: 5000 });
  await expect(page.locator("#offline")).toContainText("Unit not connected");
  await expect(page.locator("#dest-search")).toBeDisabled();
  await page.locator("#dest-search").click({ force: true, trial: false }).catch(() => {});
  expect(m.requests.filter((r) => r.method === "POST").length).toBe(before);
  await page.screenshot({ path: `${SHOT}/pilot-07-offline${proj}.png` });
  m.online = true;
  m.push("online", { at: new Date().toISOString() });
  await expect(page.locator("#offline")).toBeHidden();
  await expect(page.locator("#dest-search")).toBeEnabled();
});

test("a 409 unit_offline answer is handled gracefully", async ({ page }) => {
  const { m } = await openHome(page);
  await page.click("#dest-search");
  m.heartbeats = false; // no heartbeat may tell the page first (the heartbeat races this test under load)
  m.online = false; // the offline event has not reached the page yet
  await page.fill("#dest-search", "SunTrax");
  await page.locator(".dest-row").first().click();
  await expect(page.locator("#offline")).toBeVisible();
  await expect(page.locator("#dest-search")).toBeDisabled();
});

test("a demo vehicle-type change swaps badge and destination list within 2 s", async ({ page }) => {
  const { m } = await openHome(page);
  await page.click("#dest-search");
  await expect(page.locator(".dest-name", { hasText: "SunTrax" })).toBeVisible();
  m.push("vehicle-type", { vehicle_type: "medical", demo: true });
  await expect(page.locator("#type-badge")).toHaveText("Medical", { timeout: 2000 });
  await expect(page.locator(".dest-name", { hasText: "Tampa General" })).toBeVisible({ timeout: 2000 });
  await expect(page.locator(".dest-name", { hasText: "SunTrax" })).toHaveCount(0);
  await page.screenshot({ path: `${SHOT}/pilot-08-medical${proj}.png` });
  m.vehicleType = "medical";
  await page.locator(".dest-row", { hasText: "Tampa General" }).click();
  const req = m.requests.filter((r) => r.path.endsWith("/route-request")).pop()!;
  expect(Object.keys(req.body).sort()).toEqual(["destination", "request_id"]);
});

test("options go grey after expires_at and Refresh asks again", async ({ page }) => {
  const { m } = await openHome(page, { expiresIn: 2 });
  await requestRoute(page, "SunTrax");
  await expect(page.locator("#cards.stale")).toBeVisible({ timeout: 6000 });
  await expect(page.locator("[data-go]").first()).toBeDisabled();
  await page.screenshot({ path: `${SHOT}/pilot-09-stale${proj}.png` });
  m.expiresIn = 300;
  await page.click("#refresh");
  await expect(page.locator("#cards:not(.stale)")).toBeVisible();
  await expect(page.locator("[data-go]").first()).toBeEnabled();
  expect(m.requests.filter((r) => r.path.endsWith("/route-request")).length).toBe(2);
});

test("socket drop: polling fallback every 2 s, then the socket comes back", async ({ page }) => {
  const { m } = await openHome(page);
  m.wsAllowed = false;
  const before = m.stateCalls;
  m.drop();
  await expect(page.locator("#link")).toHaveText("Reconnecting");
  await expect.poll(() => m.stateCalls - before, { timeout: 20_000 }).toBeGreaterThanOrEqual(2);
  // offline is noticed by polling alone
  m.online = false;
  await expect(page.locator("#offline")).toBeVisible({ timeout: 5000 });
  m.online = true;
  m.wsAllowed = true;
  await expect(page.locator("#link")).toHaveText("Live", { timeout: 30_000 });
  await expect(page.locator("#offline")).toBeHidden();
  // socket back and no flight running: polling stops (at most the one repair fetch on reconnect)
  await page.waitForTimeout(500);
  const settled = m.stateCalls;
  await page.waitForTimeout(5000);
  expect(m.stateCalls - settled).toBeLessThanOrEqual(1);
});

test("heartbeat silence over 12 s: reconnecting and polling, then live again", async ({ page }) => {
  const { m } = await openHome(page);
  await expect(page.locator("#link")).toHaveText("Live");
  m.heartbeats = false; // the socket stays open but the server says nothing
  m.wsAllowed = false; // and a reconnect attempt is refused, so the state is observable
  const before = m.stateCalls;
  await expect(page.locator("#link")).toHaveText("Reconnecting", { timeout: 20_000 });
  await expect.poll(() => m.stateCalls - before, { timeout: 15_000 }).toBeGreaterThanOrEqual(1); // polling
  m.heartbeats = true; // the link recovers: reconnects work and heartbeats resume
  m.wsAllowed = true;
  await expect(page.locator("#link")).toHaveText("Live", { timeout: 20_000 });
});

test("status panel shows unit, GNSS, region, response age and network version", async ({ page }) => {
  const { m } = await openHome(page);
  await requestRoute(page, "SunTrax");
  await page.click("#back");
  await page.click("#status-btn");
  await expect(page.locator("#st-online")).toContainText("Online");
  await expect(page.locator("#st-gnss")).toContainText("HDOP 0.9");
  await expect(page.locator("#st-region")).toContainText("Inside");
  void m;
  await page.screenshot({ path: `${SHOT}/pilot-10-status${proj}.png` });
});

test("375 px: no horizontal scroll", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 700 });
  await openHome(page);
  await page.click("#dest-search");
  const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth || document.querySelector<HTMLElement>("#sheet")!.scrollWidth > window.innerWidth);
  expect(over).toBe(false);
});

// ---- E6.12: the redesigned screen in every state, day and night (screenshots for review) ----------------------------
for (const theme of ["day", "night"] as const) {
  test(`${theme}: idle, options, navigating, suggestion and offline states render`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const { m, errors } = await openHome(page);
    await expect(page.locator(".pilot")).toHaveClass(new RegExp(theme));
    const shot = (n: string) => page.screenshot({ path: `${SHOT}/pilot-carplay-${ti.project.name}-${theme}-${n}.png` });
    await page.waitForTimeout(1500);
    await shot("idle");
    await requestRoute(page, "SunTrax");
    await page.waitForTimeout(1200);
    await shot("options");
    await page.locator("[data-go]").first().click();
    await expect(page.locator("#flight-chip")).toHaveText("Planned");
    m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "active", reason: null, timestamp: new Date().toISOString() });
    await expect(page.locator("#flight-chip")).toHaveText("Active");
    await page.waitForTimeout(1000);
    await shot("navigating");
    const sel = m.requests.find((r) => r.path.endsWith("/route-selection"))!;
    const better = m.routes(m.dest!, sel.body.request_id, 1, 5);
    better.routes = [{ ...better.routes[0], est_time_s: 420 }];
    m.push("route-response", better);
    await expect(page.locator("#faster")).toBeVisible();
    await shot("suggestion");
    await page.click("#ignore");
    m.online = false;
    m.push("offline", { at: new Date().toISOString() });
    await expect(page.locator("#offline")).toBeVisible();
    await shot("offline");
    expect(errors).toEqual([]);
  });
}

test("minimal by default: no clutter on idle, a single status dot, details only on tap", async ({ page }) => {
  await openHome(page);
  await expect(page.locator(".status-dot")).toHaveClass(/ok/);
  await expect(page.locator("#status-panel")).toBeHidden();
  await expect(page.locator(".dest-row:visible")).toHaveCount(4); // nearest four until the field is used
  await page.click("#dest-search");
  expect(await page.locator(".dest-row:visible").count()).toBeGreaterThan(4);
  const box = await page.locator("#dest-search").boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(48);
});

test("End: the first tap only arms, the second ends the flight on the server and returns to idle", async ({ page }) => {
  const { m } = await openHome(page);
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  await expect(page.locator("#end-btn")).toHaveText("End");
  await page.click("#end-btn");
  await expect(page.locator("#end-btn")).toHaveText("Tap to end");
  expect(m.requests.filter((r) => r.path.endsWith("/flight/end")).length).toBe(0); // armed, nothing sent
  await page.click("#end-btn");
  await expect.poll(() => m.requests.filter((r) => r.path.endsWith("/flight/end")).length).toBe(1);
  await expect(page.locator("#dest-search")).toBeEnabled();
  await expect(page.locator("#nav")).toHaveCount(0);
  await page.waitForTimeout(5000); // polling must not bring the ended flight back
  await expect(page.locator("#nav")).toHaveCount(0);
});

test("End disarms after a few seconds without a second tap", async ({ page }) => {
  const { m } = await openHome(page);
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  await page.click("#end-btn");
  await expect(page.locator("#end-btn")).toHaveText("End", { timeout: 7000 });
  expect(m.requests.filter((r) => r.path.endsWith("/flight/end")).length).toBe(0);
});

test("long names are shortened: no parenthetical on the guidance card, full name in details", async ({ page }) => {
  const { m } = await openHome(page);
  await page.click("#dest-search");
  await expect(page.locator(".dest-name", { hasText: "SunTrax" }).first()).toHaveText("SunTrax");
  await page.fill("#dest-search", "Downtown Tampa");
  await expect(page.locator(".dest-name").first()).toHaveText("Water Street"); // "Water Street (Downtown Tampa)"
  await expect(page.locator(".dest-name").first()).toHaveAttribute("title", /Downtown Tampa/);
  void m;
});

// The real Florida basemap (basemap/florida.pmtiles, served by vite) under the pilot theme: one review screenshot per theme.
for (const theme of ["day", "night"] as const) {
  test(`${theme}: real basemap with a route (screenshot)`, async ({ page }, ti) => {
    test.skip(ti.project.name !== "pilot-phone-landscape", "one landscape run is enough");
    test.skip(!existsSync(resolve(import.meta.dirname, "../../basemap/florida.pmtiles")), "basemap/florida.pmtiles is not present");
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const m = await mockPilot(page);
    await page.unroute("**/basemap/**"); // mockApi 404s the basemap; here we want the real one
    await page.goto(URL_QR);
    await expect(page.locator("#type-badge")).toBeVisible();
    await requestRoute(page, "SunTrax");
    await page.waitForTimeout(7000);
    await page.screenshot({ path: `${SHOT}/pilot-carplay-real-${theme}.png` });
    expect(errors).toEqual([]);
    void m;
  });
}

// ---- E6.14: ATC requirement and clearance, shown to the pilot (informational, never a gate) -------------------
test("clearance: a route that crosses Class B says so, Go still works, and the pill turns green when ATC marks it", async ({ page }) => {
  const { m } = await openHome(page, { atc: { requirement: "clearance", airspaces: ["Tampa Class B"], status: "pending" } });
  await requestRoute(page, "SunTrax");
  const card = page.locator(".route-card").first();
  await expect(card.locator(".atc-pill")).toHaveText("Clearance needed (Class B)");
  await expect(card.locator(".atc-pill")).toHaveAttribute("title", "Tampa Class B");
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${SHOT}/pilot-clearance-options${proj}.png` });
  await expect(card.locator("[data-go]")).toBeEnabled(); // not a gate
  await card.locator("[data-go]").click();
  await expect(page.locator("#flight-chip")).toHaveText("Planned"); // navigating although ATC has not cleared it
  await expect(page.locator(".sheet.nav .atc-pill")).toHaveText("Clearance needed (Class B)");
  await page.screenshot({ path: `${SHOT}/pilot-clearance-pending${proj}.png` });
  // ATC marks it by radio: the server pushes a `clearance` event to this screen
  m.atc.status = "done";
  m.push("clearance", { flight_id: m.flight.flight_id, requirement: "clearance", airspaces: ["Tampa Class B"], status: "done", action: "cleared", by_role: "ATC", at: new Date().toISOString() });
  await expect(page.locator(".sheet.nav .atc-pill")).toContainText("Ops noted: ATC clearance obtained");
  await expect(page.locator(".sheet.nav .atc-pill")).toHaveClass(/done/);
  await page.screenshot({ path: `${SHOT}/pilot-clearance-cleared${proj}.png` });
  // a mark for another flight changes nothing here
  m.push("clearance", { flight_id: "someone-else", requirement: "clearance", airspaces: [], status: "pending", action: null, by_role: "ATC", at: null });
  await page.waitForTimeout(200);
  await expect(page.locator(".sheet.nav .atc-pill")).toContainText("Ops noted: ATC clearance obtained");
  // undone by ATC
  m.push("clearance", { flight_id: m.flight.flight_id, requirement: "clearance", airspaces: ["Tampa Class B"], status: "pending", action: null, by_role: "ATC", at: null });
  await expect(page.locator(".sheet.nav .atc-pill")).toHaveText("Clearance needed (Class B)");
});

test("clearance: tower contact for Class D, and nothing at all when the route needs no ATC", async ({ page }) => {
  const { m } = await openHome(page, { atc: { requirement: "contact_tower", airspaces: ["Lakeland Class D"], status: "pending" } });
  await requestRoute(page, "SunTrax");
  await expect(page.locator(".route-card").first().locator(".atc-pill")).toHaveText("Contact tower (Class D)");
  await page.locator("[data-go]").first().click();
  await expect(page.locator(".sheet.nav .atc-pill")).toHaveText("Contact tower (Class D)");
  m.atc.status = "done";
  m.push("clearance", { flight_id: m.flight.flight_id, requirement: "contact_tower", airspaces: ["Lakeland Class D"], status: "done", action: "contacted", by_role: "ATC", at: new Date().toISOString() });
  await expect(page.locator(".sheet.nav .atc-pill")).toContainText("Ops noted: tower contacted");
});

test("clearance: no pill when no ATC is needed", async ({ page }) => {
  await openHome(page);
  await requestRoute(page, "SunTrax");
  await expect(page.locator(".atc-pill")).toHaveCount(0);
  await page.locator("[data-go]").first().click();
  await expect(page.locator("#flight-chip")).toHaveText("Planned");
  await expect(page.locator(".atc-pill")).toHaveCount(0);
});

// ---- U2 story 1: predicted congestion, always on (U-API contract §1) -------------------------------------------------
const legColours = (page: Page) => page.evaluate(() => (window as any).__map.getSource("route-legs").serialize().data.features.map((f: any) => f.properties.color));
const corridorLevels = (page: Page) => page.evaluate(() => (window as any).__map.getSource("corridors").serialize().data.features.map((f: any) => f.properties.level));

test("congestion: corridors are coloured from the 15-min forecast with no flight running", async ({ page }) => {
  await openHome(page);
  await expect.poll(() => corridorLevels(page).then((l) => l.filter((x: string) => x !== "none").length)).toBeGreaterThan(0);
  const levels = await corridorLevels(page);
  // "heavy" only exists in the +15 forecast of the mock (the current level is never heavy): the 15-min value is what is drawn
  expect(levels).toContain("heavy");
  expect(levels).toContain("moderate");
  expect(await page.evaluate(() => !!(window as any).__map.getLayer("corridor-traffic"))).toBe(true);
});

test("congestion: a route's congested legs take the forecast colour; quiet legs keep the route blue", async ({ page }) => {
  const { m } = await openHome(page, { legLevels: { 1: "moderate", 2: "heavy" } });
  await requestRoute(page, "SunTrax");
  await expect.poll(() => legColours(page)).toEqual(["#f9ab00", "#d93025"]); // amber then red, in leg order
  await page.locator("[data-go]").first().click();
  await expect(page.locator("#flight-chip")).toHaveText("Planned");
  await expect.poll(() => legColours(page)).toEqual(["#f9ab00", "#d93025"]); // the same legs while navigating
  void m;
});

test("congestion: a route with no congested legs draws no leg overlay", async ({ page }) => {
  await openHome(page, { legLevels: {} });
  await requestRoute(page, "SunTrax");
  await expect(page.locator(".route-card").first()).toBeVisible();
  await expect.poll(() => legColours(page)).toEqual([]);
});

/** The short glance name of the node at `i` on the route to SunTrax (as the card shows it). */
const shortNodeName = (i: number) => (nodes.get(findPath("ev-water-st", "ev-suntrax")![i]).properties.name as string).replace(/\s*\([^)]*\)/g, "").trim();

test("congestion: the route card names the worst leg (ties to the earliest) and says Light when all legs are light", async ({ page }) => {
  await openHome(page, { legLevels: { 1: "moderate", 2: "heavy" } });
  await requestRoute(page, "SunTrax");
  const word = page.locator(".route-card").first().locator(".rc-traffic");
  await expect(word).toHaveText(`Heavy traffic near ${shortNodeName(3)}`); // leg 2 ends at path node 3
  await expect(word).toHaveClass(/heavy/);
  await page.locator(".route-card").first().locator("[data-go]").click().catch(() => {});
});

test("congestion: two equally heavy legs name the earlier one", async ({ page }) => {
  await openHome(page, { legLevels: { 1: "heavy", 2: "heavy" } });
  await requestRoute(page, "SunTrax");
  await expect(page.locator(".route-card").first().locator(".rc-traffic")).toHaveText(`Heavy traffic near ${shortNodeName(2)}`); // leg 1 ends at path node 2
});

test("congestion: all legs light reads 'Light traffic'", async ({ page }) => {
  await openHome(page, { legLevels: {} });
  await requestRoute(page, "SunTrax");
  await expect(page.locator(".route-card").first().locator(".rc-traffic")).toHaveText("Light traffic");
});

// ---- U2 story 2: the AI mark (✦) on predictions and AI reasons, and nowhere else -----------------------------------
test("AI cues: the AI mark on each ETA range and on the selected card's reason; nowhere on the idle screen", async ({ page }) => {
  await openHome(page);
  await expect(page.locator(".ai-mark")).toHaveCount(0); // idle: no AI glyph
  await requestRoute(page, "SunTrax");
  await expect(page.locator(".route-card")).toHaveCount(2);
  await expect(page.locator(".rc-eta .ai-mark")).toHaveCount(2); // one per card's ETA range
  await expect(page.locator(".route-card.selected .rc-reason .ai-mark")).toHaveCount(1);
  await expect(page.locator(".rc-reason .ai-mark:visible")).toHaveCount(1); // only the selected card's reason is shown
  await expect(page.locator(".route-card").first().locator(".rc-eta")).toContainText("Arrive");
  await expect(page.locator(".route-card").first().locator(".rc-eta .ai-mark")).toHaveCount(1);
});

test("AI cues: 'Faster route available' carries the AI mark", async ({ page }) => {
  const { m } = await openHome(page);
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  const sel = m.requests.find((r) => r.path.endsWith("/route-selection"))!;
  const better = m.routes(m.dest!, sel.body.request_id, 1, 7);
  better.routes = [{ ...better.routes[0], est_time_s: 420 }];
  m.push("route-response", better);
  await expect(page.locator("#faster")).toContainText("Faster route available, saves");
  await expect(page.locator("#faster .ai-mark")).toHaveCount(1);
});

// ---- U2 corrections: rows survive position updates, the off-route mark follows detected_by ------------------------
test("home list: a position update patches distances in place; the row you are touching is not replaced", async ({ page }) => {
  const { m } = await openHome(page);
  await page.click("#dest-search");
  const row = page.locator(".dest-row", { hasText: "SunTrax" });
  await row.evaluate((el) => { (el as any).__keep = true; });
  const before = await row.locator(".dest-dist").textContent();
  // a small move east: the distances change but the order does not (an order change may rebuild the list)
  const s0 = m.stateBody();
  s0.latest_fix = { ...s0.latest_fix, lon: s0.latest_fix.lon + 0.01 };
  m.push("state", s0);
  await page.waitForTimeout(500); // the state reaches the page and the distances are patched
  // the text shows the app's own distance for that row (NM: one decimal under 10, whole numbers above)
  const shown = await row.locator(".dest-dist").textContent();
  const want = await page.evaluate(() => { const a = (window as any).__pilot.app; const d = a.homeList().find((x: any) => x.id === "ev-suntrax").dist; const nm = d / 1852; return nm < 10 ? `${nm.toFixed(1)} NM` : `${Math.round(nm)} NM`; });
  expect(shown).toBe(want);
  expect(before).toMatch(/NM$/);
  expect(await row.evaluate((el) => (el as any).__keep)).toBe(true); // same element: nothing was rebuilt
});

test("tap survives a position update that arrives while the finger is down", async ({ page }) => {
  const { m } = await openHome(page);
  await page.click("#dest-search");
  const row = page.locator(".dest-row", { hasText: "SunTrax" });
  const box = await row.boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  m.origin = "ev-orl"; // another place: the tap still routes to SunTrax
  m.push("state", m.stateBody()); // a rebuild now would detach the row under the finger
  await page.waitForTimeout(150);
  await page.mouse.up();
  await expect(page.locator(".route-card").first()).toBeVisible(); // the tap opened the route options
  await expect.poll(() => m.requests.filter((r) => r.path.endsWith("/route-request")).length).toBe(1);
});

test("off-route: 'AI' mark only when the AI detector flagged it", async ({ page }) => {
  const { m } = await openHome(page);
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  await expect(page.locator("#flight-chip")).toHaveText("Planned");
  m.flight.status = "off_route"; // the server's state agrees with each push (a poll would otherwise undo it)
  m.flight.detected_by = "rule";
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "off_route", reason: "off", detected_by: "rule", timestamp: new Date().toISOString() });
  await expect(page.locator("#off-route")).toHaveText("Off route. Recalculating");
  m.flight.detected_by = "ai";
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "off_route", reason: "off", detected_by: "ai", timestamp: new Date().toISOString() });
  await expect(page.locator("#off-route .ai-mark")).toHaveCount(1);
  await expect(page.locator("#off-route .ai-mark")).toHaveCount(1);
});

// ---- U2 story 3: the ATC reroute card (U-API contract §2): acknowledge switches, unable keeps, nothing else changes ---
/** Fly the mock flight: plan SunTrax, Go, and mark it active. */
async function flyToSunTrax(page: Page, m: PilotMock) {
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  await expect(page.locator("#flight-chip")).toHaveText("Planned");
  m.flight.status = "active"; // the server's state agrees with the push
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "active", reason: null, timestamp: new Date().toISOString() });
  await expect(page.locator("#flight-chip")).toHaveText("Active");
}

test("ATC reroute: the card shows what and why, confirm-by-radio, the dashed preview, and the AI mark", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  m.reroute = makeReroute(m);
  m.push("reroute", m.reroute);
  const card = page.locator("#reroute");
  await expect(card).toBeVisible();
  await expect(card).toContainText("Ops reroute (advisory)");
  await expect(card).toContainText("Confirm with ATC by radio if your clearance changes");
  await expect(card).toContainText("4 min faster");
  await expect(card.locator(".ai-mark")).toHaveCount(1);
  await expect(page.locator("#rr-ack")).toBeEnabled();
  await expect(page.locator("#rr-unable")).toBeEnabled();
  // the proposal is its own purple line, and the current route is dimmed while the card is open
  const proposed = await page.evaluate(() => (window as any).__map.getSource("reroute").serialize().data.features);
  expect(proposed.length).toBe(1);
  expect(await page.evaluate(() => (window as any).__map.getPaintProperty("routes", "line-opacity"))).toBe(0.4);
  expect(await page.evaluate(() => (window as any).__map.getPaintProperty("reroute", "line-color"))).toMatch(/^#(AF52DE|BF5AF2)$/i); // system purple, day or night
  // the proposal leaves the current route after its first leg (not the same line drawn twice)
  expect(m.reroute.proposed.path[2]).not.toBe(m.flight.path[2]); // the proposal leaves the current route after the first leg
  // the reason is one line with an ellipsis; a tap shows all of it
  await expect(page.locator("#detail-sheet")).toHaveCount(0);
  await page.locator("#rr-reason").click();
  await expect(page.locator("#detail-sheet")).toBeVisible();
  await expect(page.locator("#detail-reason")).toContainText(m.reroute.reason);
  await page.click("#detail-close");
  await expect(page.locator("#detail-sheet")).toHaveCount(0);
  await page.waitForTimeout(800);
  });

test("ATC reroute: Acknowledge switches the navigation to the proposed route and clears the card", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  const rr = makeReroute(m);
  m.reroute = rr;
  m.push("reroute", rr);
  await expect(page.locator("#reroute")).toBeVisible();
  await page.click("#rr-ack");
  await expect(page.locator("#reroute")).toHaveCount(0);
  expect(m.rerouteAnswers).toEqual(["acknowledge"]);
  await expect(page.locator("#flight-chip")).toHaveText("Active"); // still flying, on the new route
  await expect.poll(() => m.flight.route_id).toBe(rr.proposed.route_id);
});

test("ATC reroute: Unable keeps the current route, says to confirm by radio, and clears the card", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  const before = m.flight.route_id;
  m.reroute = makeReroute(m);
  m.push("reroute", m.reroute);
  await expect(page.locator("#reroute")).toBeVisible();
  await page.click("#rr-unable");
  await expect(page.locator("#reroute")).toHaveCount(0);
  expect(m.rerouteAnswers).toEqual(["unable"]);
  expect(m.flight.route_id).toBe(before); // nothing changed on the route
  await expect(page.locator(".nav-msg")).toContainText("Confirm with ATC by radio");
});

test("ATC reroute: the card comes back after a reload (state from the server, not from the socket)", async ({ page }) => {
  const seed = await mockPilot(page);
  // set up a flight and an approved reroute, then reload: the card must come from GET /api/devices/{id}
  await page.goto(URL_QR);
  await expect(page.locator("#type-badge")).toBeVisible();
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  await expect(page.locator("#flight-chip")).toHaveText("Planned");
  seed.reroute = makeReroute(seed);
  await page.reload();
  await expect(page.locator("#reroute")).toBeVisible();
});

test("ATC reroute: an expired proposal is not offered", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  m.reroute = makeReroute(m, { expires_at: new Date(Date.now() - 1000).toISOString() });
  m.push("reroute", m.reroute);
  await page.waitForTimeout(600);
  await expect(page.locator("#reroute")).toHaveCount(0);
  await expect(page.locator("#rr-ack")).toHaveCount(0);
});

for (const theme of ["day", "night"] as const) {
  test(`${theme}: ATC reroute card (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const { m, errors } = await openHome(page);
    await flyToSunTrax(page, m);
    m.reroute = makeReroute(m);
    m.push("reroute", m.reroute);
    await expect(page.locator("#reroute")).toBeVisible();
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/pilot-ai-s3-card-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });
}

// ---- U2 story 4: the AI disclaimer (U-API contract §3): once after pairing, then one quiet line ----------------------
test("disclaimer: shown once after pairing; acknowledging records the version and removes it", async ({ page }) => {
  const m = await mockPilot(page, { aiAck: null });
  await page.goto(URL_QR);
  await expect(page.locator("#ai-notice")).toBeVisible();
  await expect(page.locator("#ai-notice-text")).toHaveText(AI_INFO.disclaimer.text);
  await expect(page.locator("#ai-notice")).toContainText("not a clearance");
  await page.click("#ai-ok");
  await expect(page.locator("#ai-notice")).toHaveCount(0);
  expect(m.aiAcks).toEqual([DISCLAIMER_VERSION]);
  await page.reload();
  await expect(page.locator("#type-badge")).toBeVisible();
  await expect(page.locator("#ai-notice")).toHaveCount(0); // recorded on the server: not shown again
});

test("disclaimer: the advisory line sits under the route options and in the status sheet", async ({ page }) => {
  await openHome(page);
  await expect(page.locator("#ai-advisory")).toHaveCount(0); // not on the idle screen
  await requestRoute(page, "SunTrax");
  await expect(page.locator("#ai-advisory")).toContainText("AI advisory. Not a clearance. You are pilot in command.");
  await page.click("#back");
  await page.click("#status-btn");
  await expect(page.locator("#status-panel #ai-advisory")).toContainText("AI advisory");
});

for (const theme of ["day", "night"] as const) {
  test(`${theme}: AI notice and options with the advisory line (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const { errors } = await openHome(page, { aiAck: null }, URL_QR);
    await expect(page.locator("#ai-notice")).toBeVisible();
    await page.waitForTimeout(600);
    await page.screenshot({ path: `${SHOT}/pilot-ai-s4-notice-${theme}-${ti.project.name}.png` });
    await page.click("#ai-ok");
    await requestRoute(page, "SunTrax");
    await page.waitForTimeout(600);
    await page.screenshot({ path: `${SHOT}/pilot-ai-s4-advisory-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });
}

// ---- U2 story 5: "How the AI works", from the status sheet, numbers from /api/ai -------------------------------------
test("how the AI works: four models, the measured results, and the honest route-choice note", async ({ page }) => {
  await openHome(page);
  await page.click("#status-btn");
  await page.click("#ai-how");
  const sheet = page.locator("#ai-sheet");
  await expect(sheet).toBeVisible();
  for (const title of ["Traffic & arrival-time forecaster", "Pad demand forecaster", "Off-route detector", "Route summary writer"]) await expect(sheet).toContainText(title);
  await expect(sheet).toContainText("aerolink-gbm"); // the model's own name, small
  await expect(sheet).toContainText("61 s vs 104 s");
  await expect(sheet).toContainText("84%");
  await expect(sheet).toContainText("1.00 / 0.88");
  await expect(page.locator("#ai-honest")).toHaveText(AI_INFO.results.route_choice_note);
  await expect(sheet).toContainText("AI advisory");
  await page.click("#ai-close");
  await expect(sheet).toHaveCount(0);
});

for (const theme of ["day", "night"] as const) {
  test(`${theme}: how the AI works sheet (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const { errors } = await openHome(page);
    await page.click("#status-btn");
    await page.click("#ai-how");
    await expect(page.locator("#ai-sheet")).toBeVisible();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOT}/pilot-ai-s5-how-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });
}

// ---- U2 story 6: live ADS-B traffic (contract §4): small, advisory, a tag only when close, a neutral highlight only when converging -----
/** The three ADS-B cases (U-API contract §4), around the unit at (lon0, lat0):
 *  converging and near (the one neutral highlight), near but diverging (plain, tagged), and far (plain dot-free chevron). */
const adsbCases = (lon0: number, lat0: number) => ({
  converging: { icao: "A1B2C3", callsign: "N555AA", lat: lat0 + 1852 / 111_320, lon: lon0, alt_ft: 1500, gs_kt: 120, track_deg: 180, rel_alt_ft: 500 },
  diverging: { icao: "C3D4E5", callsign: "N777BB", lat: lat0, lon: lon0 + 1852 / (111_320 * Math.cos((lat0 * Math.PI) / 180)), alt_ft: 900, gs_kt: 120, track_deg: 90, rel_alt_ft: -300 },
  far: { icao: "D4E5F6", callsign: null, lat: lat0 + (8 * 1852) / 111_320, lon: lon0, alt_ft: 1000, gs_kt: 90, track_deg: 90, rel_alt_ft: 0 },
});
const adsbFeatures = (page: Page) => page.evaluate(() => (window as any).__map.getSource("adsb").serialize().data.features.filter((f: any) => !f.properties.heading).map((f: any) => f.properties));
test("ADS-B: one neutral highlight (near AND converging, tagged 'close'); near-diverging is tagged but plain; far is plain", async ({ page }) => {
  const { m } = await openHome(page);
  const [lon0, lat0] = nodes.get("ev-water-st").geometry.coordinates as [number, number]; // the unit's fix (fixAt)
  const c = adsbCases(lon0, lat0);
  m.adsb = [c.converging, c.diverging, c.far];
  m.push("traffic", { aircraft_id: DEVICE, forecast_time: new Date().toISOString(), aircraft: [], edges: [], adsb: [c.converging, c.diverging, c.far] });
  await expect.poll(async () => (await adsbFeatures(page)).some((f: any) => f.icao === "A1B2C3")).toBe(true); // wait for this push
  const feats = await adsbFeatures(page);
  expect(feats.length).toBe(3);
  const by = (icao: string) => feats.find((f: any) => f.icao === icao);
  expect(by("A1B2C3").label).toBe("close, 500 ft above");
  expect(by("A1B2C3").close).toBe(true);
  expect(by("C3D4E5").label).toBe("300 ft below"); // near and tagged, but diverging: no highlight, no "close"
  expect(by("C3D4E5").close).toBe(false);
  expect(by("D4E5F6").label).toBe("");
  expect(by("D4E5F6").close).toBe(false);
  expect(feats.filter((f: any) => f.close).length).toBe(1); // exactly one highlighted aircraft on the map
  // neutral: no amber ring layer, and the highlight halo is grey/white, never amber or red
  expect(await page.evaluate(() => (window as any).__map.getLayer("adsb-ring"))).toBeUndefined();
  expect(await page.evaluate(() => (window as any).__map.getPaintProperty("adsb-halo", "circle-color"))).toMatch(/^#(5f6368|e8eaed)$/i);
  await page.click("#status-btn");
  await expect(page.locator("#status-panel")).toContainText("Internet ADS-B, delayed and advisory");
  await expect(page.locator("#st-adsb")).toContainText("Updated");
});

for (const theme of ["day", "night"] as const) {
  test(`${theme}: ADS-B traffic near the unit (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const { m, errors } = await openHome(page);
    const [lon0, lat0] = nodes.get("ev-water-st").geometry.coordinates as [number, number];
    const c = adsbCases(lon0, lat0);
    m.adsb = [c.converging, c.diverging, c.far];
    m.push("traffic", { aircraft_id: DEVICE, forecast_time: new Date().toISOString(), aircraft: [], edges: [], adsb: [c.converging, c.diverging, c.far] });
    await expect.poll(async () => (await adsbFeatures(page)).some((f: any) => f.icao === "A1B2C3")).toBe(true);
    // Frame the traffic (the unit is under the home sheet at the default zoom): unit and the converging aircraft (1 NM north) both in view.
    await page.evaluate(([lon, lat]) => { const map = (window as any).__map; map.fire("dragstart", { originalEvent: {} }); /* stop following the unit */ map.jumpTo({ center: [lon, lat + 0.0083], zoom: 13, bearing: 0 }); }, [lon0, lat0]);
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/pilot-ai-s6-adsb-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });
}

// ---- U2 story 7: /device connects by scanning the unit's QR code (in the page) or by typing ID and code -------------
/** A fake rear camera: a grey canvas stream. Decoded codes come from `window.__qrFake` (the scanner's test seam). */
const fakeCamera = () => {
  (navigator.mediaDevices as any).getUserMedia = async () => {
    const c = document.createElement("canvas");
    c.width = 320; c.height = 240;
    const g = c.getContext("2d")!;
    g.fillStyle = "#444"; g.fillRect(0, 0, 320, 240);
    return (c as any).captureStream(15);
  };
};
const denyCamera = () => {
  (navigator.mediaDevices as any).getUserMedia = async () => { throw new DOMException("Permission denied", "NotAllowedError"); };
};

test("/device: the connect page offers the scan, and typing the unit ID and code pairs", async ({ page }) => {
  const m = await mockPilot(page);
  await page.goto("/device");
  await expect(page.locator("h1")).toHaveText("Connect to your AeroLink unit");
  await expect(page.locator("#scan-go")).toBeVisible();
  await expect(page.locator("#manual-form")).toBeHidden();
  await page.click("#manual-toggle");
  await page.fill("#manual-unit", DEVICE);
  await page.fill("#manual-code", CODE);
  await page.click("#manual-go");
  await expect(page.locator("#type-badge")).toBeVisible();
  expect(m.pairCalls).toEqual([{ code: CODE }]);
  expect(new URL(page.url()).search).toBe("");
});

test("/device: a scanned pairing link for this site navigates to the unit and pairs", async ({ page }) => {
  const m = await mockPilot(page);
  await page.addInitScript(fakeCamera);
  await page.goto("/device");
  await page.click("#scan-go");
  await expect(page.locator("#scan-area")).toBeVisible();
  await expect(page.locator(".scan-video")).toBeVisible();
  await page.evaluate((u) => { (window as any).__qrFake = u; }, `${new URL(page.url()).origin}/device/${DEVICE}?code=${CODE}`);
  await expect(page.locator("#type-badge")).toBeVisible();
  expect(m.pairCalls).toEqual([{ code: CODE }]);
});

test("/device: a code for another site is refused with a short message; the camera keeps running", async ({ page }) => {
  await mockPilot(page);
  await page.addInitScript(fakeCamera);
  await page.goto("/device");
  await page.click("#scan-go");
  await expect(page.locator("#scan-area")).toBeVisible();
  await page.evaluate(() => { (window as any).__qrFake = "https://evil.example/device/dev-001?code=ABCD"; });
  await expect(page.locator("#scan-status")).toHaveText("That code is for another site.");
  expect(new URL(page.url()).pathname).toBe("/device");
});

test("/device: permission denied shows the fallback and opens the typed form", async ({ page }) => {
  await mockPilot(page);
  await page.addInitScript(denyCamera);
  await page.goto("/device");
  await page.click("#scan-go");
  await expect(page.locator("#scan-fallback")).toContainText("Camera permission is off");
  await expect(page.locator("#manual-form")).toBeVisible();
  await expect(page.locator("#scan-go")).toBeVisible();
});

test("per-unit pair screen: this unit's QR code pairs; another unit's code is refused", async ({ page }) => {
  const m = await mockPilot(page);
  await page.addInitScript(fakeCamera);
  await page.goto(`/device/${DEVICE}`);
  await expect(page.locator("h1")).toHaveText("Scan the unit's QR code");
  await expect(page.locator("p.muted").first()).toContainText("Camera app");
  await page.click("#pair-scan");
  await expect(page.locator("#pair-scan-area")).toBeVisible();
  await page.evaluate((c) => { (window as any).__qrFake = `${location.origin}/device/dev-002?code=${c}`; }, CODE);
  await expect(page.locator("#pair-scan-status")).toHaveText("That code is for another unit.");
  const origin = new URL(page.url()).origin;
  await page.evaluate((u) => { (window as any).__qrFake = u; }, `${origin}/device/${DEVICE}?code=${CODE}`);
  await expect(page.locator("#type-badge")).toBeVisible();
  expect(m.pairCalls).toEqual([{ code: CODE }]);
});

for (const theme of ["day", "night"] as const) {
  test(`${theme}: connect page and camera view (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    await mockPilot(page);
    await page.addInitScript(fakeCamera);
    await page.goto("/device");
    await expect(page.locator("#scan-go")).toBeVisible();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${SHOT}/pilot-device-connect-${theme}-${ti.project.name}.png` });
    await page.click("#scan-go");
    await expect(page.locator(".scan-video")).toBeVisible();
    await page.waitForTimeout(600);
    await page.screenshot({ path: `${SHOT}/pilot-device-scan-${theme}-${ti.project.name}.png` });
  });
}

// ---- QA round 2 (temp/qa/pilot/RESULTS.md, F1–F4) and contract §10 (adsb_state) ------------------------------------------
test("QA F1: with an approved reroute the camera fits both routes on every viewport (portrait included)", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  const rr = makeReroute(m);
  m.reroute = rr;
  m.push("reroute", rr);
  await expect(page.locator("#reroute")).toBeVisible();
  // every point of the current and the proposed route lies inside the visible map bounds
  const proposed = rr.proposed.waypoints; // the proposal as the server sent it (lat, lon, alt)
  await expect.poll(async () => page.evaluate((pp) => {
    const mp = (window as any).__map;
    const b = mp.getBounds();
    const cur = (window as any).__pilot.app.navRoute?.waypoints ?? [];
    return [...cur, ...pp].every(([lat, lon]: number[]) => lat >= b.getSouth() && lat <= b.getNorth() && lon >= b.getWest() && lon <= b.getEast());
  }, proposed), { timeout: 5000 }).toBe(true);
});

test("QA F2: when /api/ai is down, How the AI works says so and still shows the disclaimer", async ({ page }, ti) => {
  await mockPilot(page);
  await page.route(/\/api\/ai$/, (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{}" }));
  await page.goto(URL_QR);
  await expect(page.locator("#type-badge")).toBeVisible();
  await page.click("#status-btn");
  await page.click("#ai-how");
  await expect(page.locator("#ai-sheet")).toBeVisible();
  await expect(page.locator("#ai-unavailable")).toHaveText("AI details are unavailable right now.");
  await expect(page.locator("#ai-static-disclaimer")).toContainText("advisory only");
  await page.screenshot({ path: `${SHOT}/pilot-qa-f2-ai-unavailable-${ti.project.name}.png` });
  await page.click("#ai-close");
  await expect(page.locator("#ai-sheet")).toHaveCount(0);
});

test("QA F3: the status sheet keeps its Close control during a flight (values update in place, nothing is rebuilt)", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  await page.click("#status-btn");
  const close = page.locator("#status-close");
  await close.evaluate((el) => { (el as any).__keep = true; });
  await page.waitForTimeout(2500); // several ticks while the flight runs
  expect(await close.evaluate((el) => (el as any).__keep)).toBe(true); // the same node: not replaced under the finger
  await page.screenshot({ path: `${SHOT}/pilot-qa-f3-status-${test.info().project.name}.png` });
  await close.click();
  await expect(page.locator("#status-panel")).toBeHidden();
});

for (const state of [{ name: "stale", ok: false }, { name: "ok", ok: true }] as const) {
  test(`contract §10: adsb_state=${state.name} ${state.ok ? "shows the aircraft as nearby" : "shows no aircraft as current and says the data is unavailable"}`, async ({ page }) => {
    const { m } = await openHome(page);
    const [lon0, lat0] = nodes.get("ev-water-st").geometry.coordinates as [number, number];
    const c = adsbCases(lon0, lat0);
    m.adsb = [c.converging, c.far];
    m.adsbState = state.name;
    m.push("traffic", { aircraft_id: DEVICE, forecast_time: new Date().toISOString(), aircraft: [], edges: [], adsb: [c.converging, c.far], adsb_state: state.name });
    await page.click("#status-btn");
    if (state.ok) {
      await expect(page.locator("#st-adsb")).toContainText("Updated");
      await expect.poll(async () => (await adsbFeatures(page)).length).toBe(2);
    } else {
      await expect(page.locator("#st-adsb")).toHaveText("Traffic data unavailable");
      await expect.poll(async () => (await adsbFeatures(page)).length).toBe(0); // stale aircraft are not drawn as current
    }
  });
}

test("QA F4: a pushed off-route shows the banner at once, and back on route clears it at once", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  m.flight.status = "off_route";
  m.flight.detected_by = "rule";
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "off_route", reason: "off", detected_by: "rule", timestamp: new Date().toISOString() });
  await expect(page.locator("#off-route")).toBeVisible({ timeout: 800 }); // not waiting for the 4 s refresh
  m.flight.status = "active";
  m.flight.detected_by = null;
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "active", reason: null, timestamp: new Date().toISOString() });
  await expect(page.locator("#off-route")).toHaveCount(0, { timeout: 800 });
});

// ---- L-FIX (pilot): Ops wording, Accept / Decline, the standing notice, the credit, the disclaimer -----------------------
test("L-FIX: a cleared clearance reads 'Ops noted' with the radio line; the reroute card is Ops advisory with Accept / Decline", async ({ page }) => {
  const { m } = await openHome(page, { atc: { requirement: "clearance", airspaces: ["Tampa Class B"], status: "pending" } });
  await requestRoute(page, "SunTrax");
  await page.locator("[data-go]").first().click();
  await expect(page.locator(".sheet.nav .atc-pill")).toContainText("Clearance needed (Class B)");
  m.atc.status = "done";
  m.push("clearance", { flight_id: m.flight.flight_id, requirement: "clearance", airspaces: ["Tampa Class B"], status: "done", action: "cleared", by_role: "ops", at: new Date().toISOString() });
  await expect(page.locator(".sheet.nav .atc-pill")).toContainText("Ops noted: ATC clearance obtained");
  await expect(page.locator(".sheet.nav .atc-pill .atc-sub")).toHaveText("Confirm with ATC by radio");
  await expect(page.locator("body")).not.toContainText("Cleared by ATC");
  m.reroute = makeReroute(m);
  m.push("reroute", m.reroute);
  await expect(page.locator("#reroute")).toContainText("Ops reroute (advisory)");
  await expect(page.locator("#rr-ack")).toHaveText("Accept");
  await expect(page.locator("#rr-unable")).toHaveText("Decline");
});

test("L-FIX: the prototype notice sits at the bottom of the nav screen and covers no control", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  const note = page.locator("#proto-note");
  await expect(note).toHaveText("Prototype. Not certified. Not for flight. Not ATC.");
  const nb = await note.boundingBox();
  for (const sel of ["#end-btn", ".nav-strip", "#dest-search"]) {
    const el = page.locator(sel).first();
    if (!(await el.count())) continue;
    const b = await el.boundingBox();
    if (!b || !nb) continue;
    const overlaps = nb.x < b.x + b.width && nb.x + nb.width > b.x && nb.y < b.y + b.height && nb.y + nb.height > b.y;
    expect(overlaps, `notice must not cover ${sel}`).toBe(false);
  }
});

test("L-FIX: the pilot map and the status sheet carry the OSM, Protomaps and adsb.lol (ODbL) credit", async ({ page }) => {
  await openHome(page);
  const credit = page.locator("#map-credit");
  await expect(credit).toContainText("adsb.lol (ODbL)");
  await expect(credit).toContainText("OpenStreetMap");
  await expect(credit).toContainText("Protomaps");
  await expect(credit).toContainText("Map data from OpenStreetMap and Protomaps");
  await expect(credit.locator("a", { hasText: "OpenStreetMap" })).toHaveAttribute("href", "https://www.openstreetmap.org/copyright");
  await expect(credit.locator("a", { hasText: "OpenStreetMap" })).toHaveAttribute("target", "_blank");
  await expect(credit.locator("a", { hasText: "OpenStreetMap" })).toHaveAttribute("rel", "noopener");
  await page.click("#status-btn");
  await expect(page.locator("#status-panel .map-credit")).toContainText("adsb.lol (ODbL)");
});

test("L-FIX: the pilot notice shows disclaimer.pilot (version 2026-11-27); with only `text` from an older server it still shows", async ({ page }) => {
  const m = await mockPilot(page, { aiAck: null });
  await page.route(/\/api\/ai$/, (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...AI_INFO, disclaimer: { version: "2026-11-26", text: "Older text only." } }) }));
  await page.goto(URL_QR);
  await expect(page.locator("#ai-notice-text")).toHaveText("Older text only.");
  await page.click("#ai-ok");
  expect(m.aiAcks).toEqual(["2026-11-26"]);
});

test("L-FIX: the pilot notice reads disclaimer.pilot exactly, and says it is not a clearance", async ({ page }) => {
  await mockPilot(page, { aiAck: null });
  await page.goto(URL_QR);
  await expect(page.locator("#ai-notice-text")).toHaveText(PILOT_DISCLAIMER);
  await expect(page.locator("#ai-notice-text")).toContainText("is not certified or approved by the FAA");
});

// ---- Contract A: the reroute card says when the proposal changes the airspace requirement --------------------------------
test("contract A: the reroute card shows 'Changes your airspace requirement' only when requirement_change is not null", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  m.reroute = makeReroute(m, { requirement_change: { from: "none", to: "clearance" } });
  m.push("reroute", m.reroute);
  await expect(page.locator("#rr-req")).toHaveText("Changes your airspace requirement from No ATC to Clearance needed.");
  // a second proposal that changes nothing: no line at all
  m.reroute = makeReroute(m, { id: "rr_test_2", requirement_change: null });
  m.push("reroute", m.reroute);
  await expect(page.locator("#reroute")).toBeVisible();
  await expect(page.locator("#rr-req")).toHaveCount(0);
  // the other labels
  m.reroute = makeReroute(m, { id: "rr_test_3", requirement_change: { from: "contact_tower", to: "contact_approach" } });
  m.push("reroute", m.reroute);
  await expect(page.locator("#rr-req")).toHaveText("Changes your airspace requirement from Contact tower to Contact approach.");
});

test("L-FIX: the prototype notice is also on the home list, the route options and the status sheet", async ({ page }) => {
  await openHome(page);
  const text = "Prototype. Not certified. Not for flight. Not ATC.";
  await expect(page.locator("#proto-note")).toHaveText(text);
  await requestRoute(page, "SunTrax");
  await expect(page.locator("#cards")).toBeVisible();
  await expect(page.locator("#proto-note")).toHaveText(text);
  await page.click("#status-btn");
  await expect(page.locator("#proto-note-status")).toBeVisible();
  await expect(page.locator("#proto-note-status")).toHaveText(text);
});

for (const theme of ["day", "night"] as const) {
  test(`${theme}: L-FIX nav screen with the Ops reroute card, the prototype notice and the credit (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const { m, errors } = await openHome(page, { atc: { requirement: "clearance", airspaces: ["Tampa Class B"], status: "done" } });
    await flyToSunTrax(page, m);
    m.reroute = makeReroute(m);
    m.push("reroute", m.reroute);
    await expect(page.locator("#reroute")).toBeVisible();
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/pilot-lf-nav-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });
}

// Screenshots for review (day and night), and the same with the real Florida basemap in the landscape project.
for (const theme of ["day", "night"] as const) {
  test(`${theme}: AI congestion on corridors and route legs (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await openHome(page, { legLevels: { 1: "moderate", 2: "heavy" } });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${SHOT}/pilot-ai-s1-idle-${theme}-${ti.project.name}.png` });
    await requestRoute(page, "SunTrax");
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${SHOT}/pilot-ai-s1-options-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });
}

for (const theme of ["day", "night"] as const) {
  test(`${theme}: real basemap, AI congestion with a route (screenshot)`, async ({ page }, ti) => {
    test.skip(ti.project.name !== "pilot-phone-landscape", "one landscape run is enough");
    test.skip(!existsSync(resolve(import.meta.dirname, "../../basemap/florida.pmtiles")), "basemap/florida.pmtiles is not present");
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    await mockPilot(page, { legLevels: { 1: "moderate", 2: "heavy" } });
    await page.unroute("**/basemap/**"); // mockApi 404s the basemap; here we want the real one
    await page.goto(URL_QR);
    await expect(page.locator("#type-badge")).toBeVisible();
    await requestRoute(page, "SunTrax");
    await page.waitForTimeout(7000);
    await page.screenshot({ path: `${SHOT}/pilot-ai-s1-real-${theme}-${ti.project.name}.png` });
  });
}

// L-FIX screenshots: the notice and credit on the options and status screens, and the disclaimer (day and night).
for (const theme of ["day", "night"] as const) {
  test(`${theme}: L-FIX notice + credit on the options and status screens, and the disclaimer (screenshots)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const m = await mockPilot(page, { aiAck: null });
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(URL_QR);
    await expect(page.locator("#ai-notice-text")).toHaveText(PILOT_DISCLAIMER);
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOT}/pilot-lf-disclaimer-${theme}-${ti.project.name}.png` });
    await page.click("#ai-ok");
    await expect(page.locator("#ai-notice")).toHaveCount(0);
    expect(m.aiAcks).toEqual([DISCLAIMER_VERSION]);
    await requestRoute(page, "SunTrax");
    await expect(page.locator("#cards")).toBeVisible();
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/pilot-lf-options-${theme}-${ti.project.name}.png` });
    await page.click("#status-btn");
    await expect(page.locator("#proto-note-status")).toBeVisible();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${SHOT}/pilot-lf-status-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });

  test(`${theme}: L-FIX nav screen with a reroute that changes the airspace requirement (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const { m, errors } = await openHome(page, { atc: { requirement: "none", airspaces: [], status: "not_needed" } });
    await flyToSunTrax(page, m);
    m.reroute = makeReroute(m, { requirement_change: { from: "none", to: "clearance" } });
    m.push("reroute", m.reroute);
    await expect(page.locator("#rr-req")).toBeVisible();
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/pilot-lf-reqchange-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });
}

// ---- U5 guard: plain English, no symbols, on every main view (text, aria-labels, titles, map tags) ----------------------
const SYMBOLS = /[°±·×©‐-―•…−←-⇿─-➿⬀-⯿]/;
async function expectNoSymbols(page: Page, view: string) {
  const found = await page.evaluate(() => {
    const out: string[] = [(document.body as HTMLElement).innerText];
    document.querySelectorAll("[aria-label],[title],[placeholder]").forEach((el) => {
      for (const a of ["aria-label", "title", "placeholder"]) { const v = el.getAttribute(a); if (v) out.push(v); }
    });
    return out;
  });
  for (const t of found) expect(SYMBOLS.test(t), `${view}: symbol in "${t.slice(0, 120)}"`).toBe(false);
}

test("U5 guard: no symbols in the pilot's text on the home list, options, nav, reroute card, status sheet, AI sheet and disclaimer", async ({ page }) => {
  const m = await mockPilot(page, { aiAck: null, atc: { requirement: "clearance", airspaces: ["Tampa Class B"], status: "done" } });
  await page.goto(URL_QR);
  await expect(page.locator("#ai-notice-text")).toBeVisible();
  await expectNoSymbols(page, "disclaimer");
  await page.click("#ai-ok");
  await expect(page.locator("#ai-notice")).toHaveCount(0);
  await expectNoSymbols(page, "home");
  await requestRoute(page, "SunTrax");
  await expect(page.locator("#cards")).toBeVisible();
  await expectNoSymbols(page, "options");
  await page.locator("[data-go]").first().click();
  await expect(page.locator("#flight-chip")).toHaveText("Planned");
  m.flight.status = "active";
  m.push("flight-status", { aircraft_id: DEVICE, flight_id: m.flight.flight_id, status: "active", reason: null, timestamp: new Date().toISOString() });
  await expect(page.locator("#flight-chip")).toHaveText("Active");
  await expectNoSymbols(page, "nav");
  m.reroute = makeReroute(m, { requirement_change: { from: "none", to: "clearance" } });
  m.push("reroute", m.reroute);
  await expect(page.locator("#rr-req")).toBeVisible();
  await expectNoSymbols(page, "reroute card");
  await page.click("#status-btn");
  await expect(page.locator("#status-panel")).toBeVisible();
  await expectNoSymbols(page, "status sheet");
  await page.click("#ai-how");
  await expect(page.locator("#ai-sheet")).toBeVisible();
  await expectNoSymbols(page, "AI sheet");
});

test("U5 guard: the ADS-B map tags are plain words ('close, 500 ft above')", async ({ page }) => {
  const { m } = await openHome(page);
  const [lon0, lat0] = nodes.get("ev-water-st").geometry.coordinates as [number, number];
  const c = adsbCases(lon0, lat0);
  m.adsb = [c.converging, c.diverging, c.far];
  m.push("traffic", { aircraft_id: DEVICE, forecast_time: new Date().toISOString(), aircraft: [], edges: [], adsb: [c.converging, c.diverging, c.far] });
  await expect.poll(async () => (await adsbFeatures(page)).some((f: any) => f.icao === "A1B2C3")).toBe(true);
  for (const f of await adsbFeatures(page)) expect(SYMBOLS.test(f.label), `tag "${f.label}"`).toBe(false);
});

// ---- Short screen (phone held sideways, 932 x 430) with a reroute card that changes the requirement ----------------------
test.describe("short screen with a reroute offer", () => {
  test.use({ viewport: { width: 932, height: 430 } });
  for (const theme of ["day", "night"] as const) {
    test(`${theme}: Accept, Decline, End, the notice and the credit are all on screen and the panel does not scroll`, async ({ page }, ti) => {
      await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
      const { m, errors } = await openHome(page, { atc: { requirement: "clearance", airspaces: ["Tampa Class B"], status: "done" } });
      await flyToSunTrax(page, m);
      m.reroute = makeReroute(m, { requirement_change: { from: "none", to: "clearance" } });
      m.push("reroute", m.reroute);
      await expect(page.locator("#rr-req")).toBeVisible();
      await page.waitForTimeout(900);
      const vp = page.viewportSize()!;
      const box = async (sel: string) => (await page.locator(sel).first().boundingBox())!;
      for (const sel of ["#rr-ack", "#rr-unable", "#rr-req", ".rr-sub", "#end-btn", "#proto-note", "#map-credit"]) {
        const b = await box(sel);
        expect(b, `${sel} has a box`).not.toBeNull();
        expect(b.x, `${sel} left`).toBeGreaterThanOrEqual(0);
        expect(b.y, `${sel} top`).toBeGreaterThanOrEqual(0);
        expect(b.x + b.width, `${sel} right`).toBeLessThanOrEqual(vp.width);
        expect(b.y + b.height, `${sel} bottom`).toBeLessThanOrEqual(vp.height);
      }
      // the notice and the credit wrap inside the panel instead of running past its edge
      const panel = await box(".panel-col");
      for (const sel of ["#proto-note", "#map-credit"]) {
        const b = await box(sel);
        expect(b.x + b.width, `${sel} inside the panel`).toBeLessThanOrEqual(panel.x + panel.width + 1);
      }
      // nothing scrolls: the sheet shows all of its content
      const sheet = await page.evaluate(() => { const el = document.querySelector(".sheet")!; return { sh: el.scrollHeight, ch: el.clientHeight }; });
      expect(sheet.sh, "the panel does not scroll").toBeLessThanOrEqual(sheet.ch + 1);
      await page.screenshot({ path: `${SHOT}/pilot-lf-short-reqchange-${theme}-${ti.project.name}.png` });
      expect(errors).toEqual([]);
    });
  }
});

// ---- Cut-off text opens a details sheet (user decision): chevron cue, full text, the card stays ---------------------------
const LONG_REASON = "Predicted congestion at Kissimmee between 7:42 PM and 7:50 PM, a possible conflict with SIM-P03 near the northern corridor, and a thunderstorm cell moving across the direct route, so this longer way round should save time overall";
test("cut-off reason: the chevron shows, a tap opens Reroute details with the full text, and closing it keeps the card", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  m.reroute = makeReroute(m, { reason: LONG_REASON, requirement_change: { from: "none", to: "clearance" } });
  m.push("reroute", m.reroute);
  await expect(page.locator("#reroute")).toBeVisible();
  const cut = await page.locator("#rr-reason .rr-reason-text").evaluate((el) => el.scrollWidth > el.clientWidth + 1);
  expect(cut, "the long reason is cut off on the card").toBe(true);
  await expect(page.locator("#rr-reason .cut-chev")).toBeVisible();
  await page.locator("#rr-reason").click();
  const sheet = page.locator("#detail-sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet.locator("h2")).toHaveText("Reroute details");
  await expect(page.locator("#detail-reason")).toContainText(LONG_REASON);
  await expect(page.locator("#detail-reason .ai-mark")).toHaveCount(1);
  await expect(page.locator("#detail-gain")).toHaveText("4 min faster");
  await expect(page.locator("#detail-req")).toHaveText("Changes your airspace requirement from No ATC to Clearance needed.");
  await expect(page.locator("#detail-sub")).toHaveText("Confirm with ATC by radio if your clearance changes");
  await expectNoSymbols(page, "reroute details");
  await page.click("#detail-close");
  await expect(sheet).toHaveCount(0);
  await expect(page.locator("#reroute")).toBeVisible();
  await expect(page.locator("#rr-ack")).toBeEnabled();
  expect(m.rerouteAnswers).toEqual([]); // nothing was answered by looking at the details
});

test("cut-off reroute details: Accept in the sheet answers the reroute", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  m.reroute = makeReroute(m);
  m.push("reroute", m.reroute);
  await page.locator("#rr-reason").click();
  await page.click("#detail-accept");
  await expect(page.locator("#detail-sheet")).toHaveCount(0);
  await expect.poll(() => m.rerouteAnswers).toEqual(["acknowledge"]);
});

test("cut-off name: a clamped next stop gets a chevron and opens the details sheet with the whole name", async ({ page }) => {
  const { m } = await openHome(page);
  await flyToSunTrax(page, m);
  const FULL = "Extraordinarily Long Vertiport Name For Testing Cut Off Text Handling In The Pilot Navigation Card Heading";
  await page.evaluate((t) => { const el = document.querySelector("#next-name") as HTMLElement; el.textContent = t; el.setAttribute("data-full", t); }, FULL);
  await expect(page.locator("#next-name.cut .cut-chev")).toBeVisible({ timeout: 5000 });
  await page.locator("#next-name").click();
  await expect(page.locator("#detail-full")).toHaveText(FULL);
  await expect(page.locator("#detail-sheet h2")).toHaveText("Next stop");
  await page.click("#detail-close");
  await expect(page.locator("#detail-sheet")).toHaveCount(0);
});

for (const theme of ["day", "night"] as const) {
  test(`${theme}: Reroute details sheet (screenshot)`, async ({ page }, ti) => {
    await page.addInitScript((t) => { try { localStorage.setItem("aerolink.pilot.theme", t); } catch { /* blocked */ } }, theme);
    const { m, errors } = await openHome(page);
    await flyToSunTrax(page, m);
    m.reroute = makeReroute(m, { reason: LONG_REASON, requirement_change: { from: "none", to: "clearance" } });
    m.push("reroute", m.reroute);
    await page.locator("#rr-reason").click();
    await expect(page.locator("#detail-sheet")).toBeVisible();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOT}/pilot-lf-detailsheet-${theme}-${ti.project.name}.png` });
    expect(errors).toEqual([]);
  });
}
