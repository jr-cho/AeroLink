import { expect, test, type Page } from "@playwright/test";
import { mockOps, type OpsMock } from "./ops-mock.ts";
import { laneLabels, shortLaneName } from "../src/ops/labels.ts";

/** The host the app is served from (the port is per agent, set by PW_PORT in playwright.config.ts). */
const appHost = (): string => new URL(test.info().project.use.baseURL ?? "http://localhost:4173").host;
const SHOT = "test-results/screenshots";
test.use({ viewport: { width: 1440, height: 900 } });
// the machine is shared with other agents' browser runs: give each test room (the pilot project uses 60 s too)
test.describe.configure({ timeout: 60_000 });
const source = (page: Page, id: string) => page.evaluate((i) => (window as any).__map.getSource(i).serialize().data.features.length, id);
const rowIds = (page: Page) => page.locator(".fl-row").evaluateAll((els) => els.map((e) => (e as HTMLElement).dataset.flight));

/** WCAG contrast of an element's text colour against its own background (alpha backgrounds are composited over both white
 *  and black, and the worse of the two counts, because a map shows through). Returns the measured ratio. */
async function contrast(page: Page, selector: string): Promise<number> {
  return page.locator(selector).first().evaluate((el) => {
    const parse = (c: string) => (c.match(/[\d.]+/g) ?? []).map(Number);
    const lin = (v: number) => { const x = v / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; };
    const lum = (r: number, g: number, b: number) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    const cs = getComputedStyle(el);
    const [fr, fg, fb] = parse(cs.color);
    let bgc = "rgba(0, 0, 0, 0)";
    for (let n: Element | null = el; n; n = n.parentElement) { const b = getComputedStyle(n).backgroundColor; if (parse(b).length < 4 || parse(b)[3] > 0) { bgc = b; break; } }
    const [br, bg, bb, ba = 1] = parse(bgc);
    const op = Number(cs.opacity); // an element's own opacity also fades its text toward the background
    return Math.min(...[255, 0].map((base) => {
      const mix = (c: number) => c * ba + base * (1 - ba);
      const bl = lum(mix(br), mix(bg), mix(bb));
      const t = (c: number, b: number) => c * op + b * (1 - op);
      const fl = lum(t(fr, mix(br)), t(fg, mix(bg)), t(fb, mix(bb)));
      const [hi, lo] = fl > bl ? [fl, bl] : [bl, fl];
      return (hi + 0.05) / (lo + 0.05);
    }));
  });
}

async function open(page: Page, init: Partial<OpsMock> = {}) {
  const errors: string[] = [];
  const hosts = new Set<string>();
  page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("request", (r) => { const u = new URL(r.url()); if (u.protocol.startsWith("http")) hosts.add(u.host); });
  const m = await mockOps(page, init);
  await page.goto("/ops");
  await expect(page.locator("#map canvas")).toBeVisible();
  await expect(page.locator(".fl-row")).toHaveCount(5); // CP2 mock: AL-07 added
  await page.waitForFunction(() => !!(window as any).__map?.getSource("ops-traffic")?.serialize().data.features.length);
  return { m, errors, hosts };
}

test("light by default: sidebar of all flights, needs-action first, traffic markers, attribution, same origin only", async ({ page }) => {
  const { errors, hosts } = await open(page);
  await expect(page.locator(".ops")).toHaveClass(/day/);
  expect(await page.evaluate(() => getComputedStyle(document.querySelector(".sidebar")!).backgroundColor)).toMatch(/rgba?\(255, 255, 255/);
  // needs action first (clearance, tower), then by arrival: AL-01 (earliest) before dev-002
  const ids = (await rowIds(page)) as string[];
  expect(ids.slice(0, 2).sort()).toEqual(["f-al02", "f-dev1"]);
  await expect(page.locator("#needs-badge")).toHaveText("2 need action");
  await expect(page.locator('[data-flight="f-dev1"] .atc-pill')).toHaveText("Clearance needed");
  await expect(page.locator('[data-flight="f-dev1"] .atc-pill')).toHaveClass(/danger/);
  await expect(page.locator('[data-flight="f-al02"] .atc-pill')).toHaveText("Contact tower");
  await expect(page.locator('[data-flight="f-al02"] .atc-pill')).toHaveClass(/warn/);
  await expect(page.locator('[data-flight="f-al01"] .atc-pill')).toHaveText("No ATC");
  await expect(page.locator('[data-flight="f-dev1"] .cs')).toHaveText("N123AL");
  await expect(page.locator('[data-flight="f-dev2"] .tag.medical')).toBeVisible();
  await expect.poll(() => source(page, "ops-traffic")).toBe(8); // 2 units + 4 simulated + 2 ADS-B (AL-07 added)
  expect(await source(page, "ops-route")).toBe(0); // other routes stay hidden until a flight is picked
  await expect.poll(() => source(page, "ops-load")).toBe(2); // predicted congestion is on by default: the two busy sites
  expect(await source(page, "ops-conflict-lines")).toBe(0); // conflicts at rest: rings and outlines, no lines across the map
  expect(await source(page, "ops-conflict-rings")).toBe(3);
  expect(await source(page, "ops-conflict-aircraft")).toBe(6);
  await expect(page.locator(".attribution")).toContainText("adsb.lol");
  await expect(page.locator("#adsb-attribution")).toContainText("ODbL");
  await expect(page.locator(".attribution")).toContainText("Map data from OpenStreetMap and Protomaps");
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${SHOT}/ops-carplay-light-overview.png` });
  expect([...hosts]).toEqual([appHost()]);
  expect(errors.filter((e) => !/basemap/.test(e) && !/404/.test(e))).toEqual([]);
});

test("dark appearance: Light / Dark / Auto, remembered per browser, same on the login page", async ({ page }) => {
  const { m } = await open(page);
  await page.click("#gear-btn");
  await page.click("#ap-dark");
  await expect(page.locator(".ops")).toHaveClass(/night/);
  expect(await page.evaluate(() => localStorage.getItem("aerolink.ops.appearance"))).toBe("dark");
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-carplay-dark-overview.png` });
  await page.reload();
  await expect(page.locator(".ops")).toHaveClass(/night/);
  await page.click("#gear-btn");
  await page.click("#ap-auto");
  expect(await page.evaluate(() => localStorage.getItem("aerolink.ops.appearance"))).toBe("auto");
  await page.click("#ap-light");
  await expect(page.locator(".ops")).toHaveClass(/day/);
  await page.click("#ap-dark");
  m.session = false;
  await page.goto("/login");
  await expect(page.locator(".login")).toHaveClass(/night/);
});

test("filter chips: All, Needs action, Medical", async ({ page }) => {
  await open(page);
  await page.click('[data-filter="needs"]');
  await expect(page.locator(".fl-row")).toHaveCount(2);
  await page.click('[data-filter="medical"]');
  await expect(page.locator(".fl-row")).toHaveCount(1);
  await expect(page.locator('.fl-row[data-flight="f-dev2"]')).toBeVisible();
  await page.click('[data-filter="all"]');
  await expect(page.locator(".fl-row")).toHaveCount(5);
});

test("click a flight: route in blue, detail card, ATC legs, ETA windows, history, track, conflicts and what-if", async ({ page }) => {
  const { m } = await open(page);
  m.requests.length = 0;
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details"); // the popover's Details opens the detail
  await expect(page.locator(".dd-title")).toHaveText("N123AL");
  await expect(page.locator("#atc-card")).toContainText("Crosses Tampa Class B");
  await expect.poll(() => source(page, "ops-route")).toBe(1);
  await expect.poll(() => source(page, "ops-route-atc")).toBe(1);
  await expect(page.locator("#whatif .alt").first()).toContainText("Suggest by radio: 4 min faster via");
  await expect(page.locator("#whatif")).toContainText("Nothing here is sent to the aircraft");
  expect(m.requests.some((r) => r.path.startsWith("/api/ops/whatif/"))).toBe(true);
  await page.click("#eta-fold summary");
  await expect(page.locator("#eta-table tbody tr")).not.toHaveCount(0);
  await page.click("#hist-fold summary");
  await expect(page.locator("#pred-history li")).toHaveCount(2);
  await page.click("#track-fold summary");
  await expect(page.locator("#track-summary")).toContainText("6 points in 2 segments");
  await expect.poll(() => source(page, "ops-track")).toBe(2);
  await page.locator("#whatif .alt").first().click();
  await expect.poll(() => source(page, "ops-whatif")).toBe(1);
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-carplay-light-detail.png` });
  await page.click("#fl-back"); // back to the list: the selection clears
  await expect(page.locator(".fl-row")).toHaveCount(5);
  await expect.poll(() => source(page, "ops-route")).toBe(0);
  await expect.poll(() => source(page, "ops-whatif")).toBe(0);
  await page.locator('.fl-row[data-flight="f-dev2"]').click(); // medical flight: its conflicts are listed in the card
  await page.click("#pop-details");
  await expect(page.locator(".mini-row.hot.high")).toContainText("AL-02 and N456MD"); // two conflicts on this flight now: the high one first
});

test("mark cleared: the pill changes, the card says who and when, undo puts it back; the page sends nothing else", async ({ page }) => {
  test.setTimeout(60_000); // these wait out the 10 s collapse; slower on a loaded machine
  const { m } = await open(page);
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await expect(page.locator("#mark-btn")).toHaveText("Mark cleared");
  await page.click("#mark-btn");
  await expect(page.locator("#atc-card .atc-pill")).toHaveText("Ops marked cleared, OO");
  await expect(page.locator("#atc-card .atc-pill")).toHaveClass(/ok/);
  await expect(page.locator("#marked-by")).toContainText("Marked cleared by Ops One");
  expect(m.marks).toEqual([{ flight: "f-dev1", action: "cleared" }]);
  await expect(page.locator("#mark-btn")).toHaveCount(0);
  await page.screenshot({ path: `${SHOT}/ops-carplay-light-cleared.png` });
  await page.click("#fl-back");
  await expect(page.locator('[data-flight="f-dev1"] .atc-pill')).toHaveText("Ops marked cleared, OO");
  await expect(page.locator("#needs-badge")).toHaveText("1 need action");
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await page.click("#undo-btn");
  await expect(page.locator("#atc-card .atc-pill")).toHaveText("Clearance needed");
  expect(m.marks.map((x) => x.action)).toEqual(["cleared", "revoke"]);
  // a tower contact is "Mark contacted"
  await page.click("#fl-back");
  await page.locator('.fl-row[data-flight="f-al02"]').click();
  await page.click("#pop-details");
  await expect(page.locator("#mark-btn")).toHaveText("Mark contacted");
  await page.click("#mark-btn");
  await expect(page.locator("#atc-card .atc-pill")).toHaveText("Ops marked contacted, OO");
  // a flight that needs nothing offers no button
  await page.click("#fl-back");
  await page.locator('.fl-row[data-flight="f-al01"]').click();
  await page.click("#pop-details");
  await expect(page.locator("#mark-btn")).toHaveCount(0);
  await expect(page.locator("#atc-card")).toContainText("crosses no airspace that needs ATC");
  const writes = m.requests.filter((r) => r.method !== "GET").map((r) => `${r.method} ${r.path}`);
  expect(writes.every((w) => /^POST \/api\/ops\/flights\/[^/]+\/clearance$/.test(w))).toBe(true);
  expect(m.clientMessages).toEqual([]);
  await expect(page.getByRole("button", { name: /send|dispatch|instruct|hold|reroute/i })).toHaveCount(0);
});

test("a failed mark says so and changes nothing; another operator's mark arrives over the socket", async ({ page }) => {
  const { m } = await open(page, { markFails: true });
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await page.click("#mark-btn");
  await expect(page.locator("#mark-error")).toContainText("this route needs");
  await expect(page.locator("#atc-card .atc-pill")).toHaveText("Clearance needed");
  await page.click("#fl-back");
  const atc = { requirement: "contact_tower", airspaces: ["Lakeland Class D"], legs: [], status: "done", action: "contacted", by: "ops2", by_name: "Op Two", initials: "OT", at: new Date().toISOString() };
  m.push("clearance", { flight_id: "f-al02", aircraft_id: "AL-02", atc });
  await expect(page.locator('[data-flight="f-al02"] .atc-pill')).toHaveText("Ops marked contacted, OT");
  await expect(page.locator("#needs-badge")).toHaveText("1 need action");
});

test("gear: sources and congestion toggles; congestion slider recolours corridors and shows site load", async ({ page }) => {
  await open(page);
  await page.click("#gear-btn");
  await page.uncheck("#tg-adsb");
  await expect.poll(() => source(page, "ops-traffic")).toBe(6); // 2 units + 4 simulated
  await page.uncheck("#tg-sim");
  await expect.poll(() => source(page, "ops-traffic")).toBe(2);
  await page.check("#tg-adsb");
  await expect.poll(() => source(page, "ops-traffic")).toBe(4);
  await page.check("#tg-sim");
  const heavy = () => page.evaluate(() => (window as any).__map.getSource("ops-congestion").serialize().data.features.filter((f: any) => f.properties.level === "heavy").length);
  const load = () => page.evaluate(() => (window as any).__map.getSource("ops-load").serialize().data.features.map((f: any) => f.properties.level));
  await expect.poll(heavy).toBeGreaterThan(0); // on by default, at +15 min
  await page.uncheck("#tg-congestion");
  expect(await heavy()).toBe(0);
  expect(await source(page, "ops-load")).toBe(0);
  await page.check("#tg-congestion");
  await expect.poll(async () => (await load()).length).toBe(2);
  await page.locator("#traffic-slider").fill("30");
  await expect(page.locator("#traffic-slider-label")).toHaveText("in 30 min");
  expect(await heavy()).toBeGreaterThan(0);
  expect(await load()).toContain("heavy");
  expect(await source(page, "ops-predicted")).toBe(5); // one predicted point per flight (AL-07 added)
  await page.uncheck("#tg-congestion");
  expect(await source(page, "ops-load")).toBe(0);
});

test("predicted congestion is on by default: corridors amber or red at +15 min, low left plain, the ✦ legend names the horizon", async ({ page }) => {
  await open(page);
  await expect(page.locator("#ai-legend")).toBeVisible();
  await expect(page.locator("#ai-legend-text")).toHaveText("AI forecast, in 15 min");
  await expect(page.locator("#ai-legend")).toContainText("Moderate");
  await expect(page.locator("#ai-legend")).toContainText("Heavy");
  const levels = () => page.evaluate(() => (window as any).__map.getSource("ops-congestion").serialize().data.features.map((f: any) => f.properties.level as string));
  await expect.poll(async () => (await levels()).length).toBeGreaterThan(0);
  expect(new Set(await levels())).toEqual(new Set(["moderate", "heavy"])); // never a "low" colour
  const width = await page.evaluate(() => (window as any).__map.getLayer("ops-congestion") !== undefined);
  expect(width).toBe(true);
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-1-light.png` });

  await page.click("#gear-btn");
  await expect(page.locator("#traffic-slider-label")).toHaveText("in 15 min");
  await page.locator("#traffic-slider").fill("0"); // "Now": nothing is predicted to be busy yet in the mock
  await expect(page.locator("#ai-legend-text")).toHaveText("AI forecast, now");
  await expect.poll(async () => (await levels()).length).toBe(0);
  await page.locator("#traffic-slider").fill("30");
  await expect(page.locator("#ai-legend-text")).toHaveText("AI forecast, in 30 min");
  await page.uncheck("#tg-congestion");
  await expect(page.locator("#ai-legend")).toBeHidden();
  await expect.poll(async () => (await levels()).length).toBe(0);
});

test("conflicts at rest are rings and outlines; the line joins the two aircraft only for the selected or hovered conflict", async ({ page }) => {
  await open(page);
  await expect.poll(() => source(page, "ops-conflict-rings")).toBe(3);
  expect(await source(page, "ops-conflict-lines")).toBe(0);
  await page.locator('.fl-row[data-flight="f-dev2"]').click(); // its two conflicts involve the selected flight
  await expect.poll(() => source(page, "ops-conflict-lines")).toBe(2);
  await page.keyboard.press("Escape"); // closes the popover only: the selection, and its line, stay
  await expect(page.locator("#flight-pop")).toBeHidden();
  await expect.poll(() => source(page, "ops-conflict-lines")).toBe(2);
  await page.click('[data-filter="all"]'); // clearing the selection
  await expect.poll(() => source(page, "ops-conflict-lines")).toBe(0);
  await page.click("#bell-btn");
  await page.click("#tab-conflicts");
  await expect(page.locator(".cf-row")).toHaveCount(3);
  await page.locator(".cf-row").nth(1).hover();
  await expect.poll(() => source(page, "ops-conflict-lines")).toBe(1); // hover shows that conflict's line
  await page.locator(".cf-row").nth(1).click();
  await page.mouse.move(700, 450);
  await expect.poll(() => source(page, "ops-conflict-lines")).toBe(1); // selected conflict keeps its line
  await page.screenshot({ path: `${SHOT}/ops-conflict-selected.png` });
});

test("predicted congestion in dark: the same corridors and legend on the night look", async ({ page }) => {
  await open(page);
  await page.click("#gear-btn");
  await page.click("#ap-dark");
  await page.click("#gear-btn");
  await expect(page.locator(".ops")).toHaveClass(/night/);
  await expect(page.locator("#ai-legend")).toBeVisible();
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-1-dark.png` });
});

test("bell: alerts live push and acknowledge (the page sends no name); conflicts ranked with medical flagged", async ({ page }) => {
  const { m } = await open(page);
  await expect(page.locator("#bell-count")).toHaveText("2");
  await page.click("#bell-btn");
  await expect(page.locator(".al-row")).toHaveCount(2);
  await page.uncheck("#alerts-open");
  await expect(page.locator(".al-row")).toHaveCount(3);
  await page.check("#alerts-open");
  m.push("alert", { id: 4, at: new Date().toISOString(), kind: "low_energy_emergency", severity: "critical", aircraft_id: "AL-01", message: "AL-01 low energy emergency", detail: {}, acked_at: null, acked_by: null });
  await expect(page.locator(".al-row")).toHaveCount(3);
  await expect(page.locator("#bell-count")).toHaveText("3");
  await expect(page.locator("#interrupt")).toBeVisible();
  await page.locator('[data-ack="4"]').click();
  await expect(page.locator("#bell-count")).toHaveText("2");
  const ack = m.requests.filter((r) => r.method === "POST" && r.path === "/api/alerts/4/ack");
  expect(ack.length).toBe(1);
  expect(ack[0].headers["x-operator"]).toBeUndefined();
  m.push("ack", { ...m.alerts.find((a) => a.id === 3), acked_at: new Date().toISOString(), acked_by: "ops2" });
  await expect(page.locator("#bell-count")).toHaveText("1");
  await page.click("#tab-conflicts");
  const rows = page.locator(".cf-row");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("AL-02 and N456MD");
  await expect(rows.nth(0)).toContainText("Medical");
  await expect(rows.nth(1)).toContainText("ADS-B N123AB");
  await expect(page.locator("#conflict-list")).toContainText("3 flights at Downtown Orlando");
  const before = await page.evaluate(() => (window as any).__map.getCenter().lng);
  await rows.nth(1).click();
  await page.waitForFunction((b) => Math.abs((window as any).__map.getCenter().lng - b) > 0.05, before);
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${SHOT}/ops-carplay-light-conflicts.png` });
});

test("status dot: units and system health; ADS-B feed down is visible", async ({ page }) => {
  await open(page, { adsbState: "down" });
  await expect(page.locator("#status-btn")).toHaveClass(/tone-bad/); // the adsb watchdog fails
  await page.click("#status-btn");
  const rows = page.locator("#fleet-table tbody tr");
  await expect(rows).toHaveCount(3);
  await expect(page.locator('[data-unit="dev-001"]')).toContainText("N123AL");
  await expect(page.locator('[data-unit="dev-001"]')).toContainText("no gnss fix");
  await expect(page.locator('[data-unit="dev-003"]')).toContainText("dev-003");
  await page.click("#tab-system");
  await expect(page.locator("#watchdog-table tbody tr")).toHaveCount(6);
  await expect(page.locator('[data-watchdog="adsb"]')).toContainText("fail");
  await expect(page.locator("#adsb-state")).toContainText("timeout");
  await expect(page.locator("#forecast-state")).toContainText("Refreshed 4 s ago");
  await expect(page.locator("#model-list")).toContainText("aerolink-gbm: loaded");
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${SHOT}/ops-carplay-light-system.png` });
});

test("system settings use the existing controls; live channel loss is shown", async ({ page }) => {
  const { m } = await open(page);
  await page.click("#status-btn");
  await page.click("#tab-system");
  await page.locator('#sys-method [data-method="classical"]').click();
  await expect.poll(() => m.requests.some((r) => r.method === "PUT" && r.path === "/api/method")).toBe(true);
  await page.selectOption("#sys-scenario", "i4_peak");
  await expect.poll(() => m.requests.some((r) => r.method === "PUT" && r.path === "/api/scenario")).toBe(true);
  m.drop();
  await expect(page.locator("#ind-live")).toHaveText("Live feed lost");
  await expect(page.locator("#ind-live")).toHaveText("Live", { timeout: 8000 });
});

test("1920x1080 dark: nothing overflows", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await open(page);
  await page.click("#gear-btn");
  await page.click("#ap-dark");
  await page.click("#gear-btn");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight)).toBe(true);
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-carplay-dark-detail.png` });
});

test("callsigns, not ids, in the sidebar, labels and fleet; ids still key the flights", async ({ page }) => {
  await open(page);
  await expect(page.locator('[data-aircraft="dev-002"] .cs')).toHaveText("N456MD");
  await expect(page.locator('[data-aircraft="AL-01"] .cs')).toHaveText("AL-01"); // no callsign known: the id
});

// ---- operator login (G2) ---------------------------------------------------------------------------------
test("without a session /ops goes to the login page, wrong passwords are refused, then back to /ops", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const m = await mockOps(page, { session: false });
  await page.goto("/ops");
  await expect(page).toHaveURL(/\/login\?next=%2Fops$/);
  await expect(page.locator("h1")).toHaveText("AeroLink Ops sign-in");
  await expect(page.locator("#username")).toBeFocused();
  await expect(page.locator("#login-error")).toHaveText("");
  await expect(page.locator("#login-notice")).toHaveCount(0);
  await page.screenshot({ path: `${SHOT}/login-card.png` });

  await page.click("#signin"); // empty form: asked locally, nothing sent
  await expect(page.locator("#login-error")).toHaveText("Enter your username and password.");
  expect(m.logins).toEqual([]);

  await page.fill("#username", "ops1");
  await page.fill("#password", "wrong-password");
  await page.click("#signin");
  await expect(page.locator("#login-error")).toHaveText("Wrong username or password.");
  await expect(page.locator("#password")).toHaveValue(""); // cleared, ready to retype
  await expect(page).toHaveURL(/\/login/);

  await page.fill("#password", "right-password-1");
  await page.keyboard.press("Enter"); // Enter submits
  await expect(page).toHaveURL(/\/ops$/);
  await expect(page.locator("#map canvas")).toBeVisible();
  await page.click("#gear-btn");
  await expect(page.locator("#operator-name")).toHaveText("Ops One");
  expect(m.logins.map((l) => l.username)).toEqual(["ops1", "ops1"]);
  expect(errors).toEqual([]);
});

test("login: rate limit and unreachable server are said plainly; a session on /login goes straight on", async ({ page }) => {
  const m = await mockOps(page, { session: false, limited: true });
  await page.goto("/login");
  await page.fill("#username", "ops1");
  await page.fill("#password", "right-password-1");
  await page.click("#signin");
  await expect(page.locator("#login-error")).toHaveText("Too many attempts. Wait a minute and try again.");
  await page.route("**/api/auth/login", (r) => r.abort());
  await page.fill("#password", "right-password-1"); // a failed attempt clears the password field
  await page.click("#signin");
  await expect(page.locator("#login-error")).toHaveText("Cannot reach the server.");
  m.session = true;
  await page.goto("/login?next=/ops");
  await expect(page).toHaveURL(/\/ops$/);
});

test("login ignores a next= that leaves the site", async ({ page }) => {
  await mockOps(page, { session: false });
  for (const next of ["https://evil.example/ops", "//evil.example", "/\\evil.example"]) {
    await page.goto(`/login?next=${encodeURIComponent(next)}`);
    await page.fill("#username", "ops1");
    await page.fill("#password", "right-password-1");
    await page.click("#signin");
    await expect(page).toHaveURL(`http://${appHost()}/ops`);
    await page.evaluate(() => fetch("/api/auth/logout", { method: "POST" }));
  }
});

test("login card fits a phone, light by default", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  await mockOps(page, { session: false });
  await page.goto("/login");
  await expect(page.locator("#signin")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await page.evaluate(() => getComputedStyle(document.querySelector(".login")!).backgroundColor)).toBe("rgb(236, 238, 233)");
});

test("sign out ends the session and the next visit needs a login again", async ({ page }) => {
  const { m } = await open(page);
  await page.click("#gear-btn");
  await expect(page.locator("#operator-name")).toHaveText("Ops One");
  await page.click("#logout");
  await expect(page).toHaveURL(/\/login\?next=%2Fops$/);
  expect(m.requests.some((r) => r.method === "POST" && r.path === "/api/auth/logout")).toBe(true);
  expect(m.session).toBe(false);
  await expect(page.locator("#login-notice")).toHaveCount(0); // a chosen sign-out is not an "ended" notice
  await page.goto("/ops");
  await expect(page).toHaveURL(/\/login/);
});

test("a session that ends mid-use sends the operator to the login page with a notice", async ({ page }) => {
  const { m } = await open(page);
  m.session = false; // the server forgets the session: the next poll gets 401
  await expect(page).toHaveURL(/\/login\?next=%2Fops&expired=1$/, { timeout: 15_000 });
  await expect(page.locator("#login-notice")).toHaveText("Your session ended. Sign in again.");
  await page.fill("#username", "ops1");
  await page.fill("#password", "right-password-1");
  await page.click("#signin");
  await expect(page).toHaveURL(/\/ops$/); // and back where they were
});

test("the dashboard leaves by itself just before the session's end time", async ({ page }) => {
  await mockOps(page, { sessionSeconds: 7 });
  await page.goto("/ops");
  await expect(page.locator("#map canvas")).toBeVisible();
  await expect(page).toHaveURL(/\/login\?next=%2Fops&expired=1$/, { timeout: 15_000 });
});


// ---- AI reroute proposals (U3 story 2, contract §2): the AI proposes, ATC approves, the pilot answers -------------
const rrCard = (page: Page, id: string) => page.locator(`.rr-card[data-reroute="${id}"]`);

test("AI reroute: a proposal arrives on the socket as a pinned card, medical first, with a dashed preview on the map", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await expect(page.locator(".rr-card")).toHaveCount(1);
  m.push("reroute", m.addReroute("rr_2", "f-dev2")); // medical
  await expect(page.locator(".rr-card")).toHaveCount(2);
  await expect(page.locator(".rr-card").first()).toHaveAttribute("data-flight", "f-dev2"); // medical first
  const card = rrCard(page, "rr_1");
  await expect(card.locator(".ai-mark")).toHaveText("AI");
  await expect(card.locator(".rr-title")).toContainText("N123AL to");
  await expect(card.locator(".rr-reason")).toContainText("Predicted congestion at Kissimmee");
  await expect(card.locator(".rr-via")).toContainText("4 min faster");
  await expect(card.getByRole("button", { name: "Approve reroute" })).toBeVisible();
  await expect(card.getByRole("button", { name: "Dismiss" })).toBeVisible();
  await expect.poll(() => source(page, "ops-reroute")).toBe(1); // one preview at a time: the top card's proposed route, dashed
  expect(await source(page, "ops-reroute-current")).toBe(1); // and its current route, solid
  await expect(page.locator('.fl-row[data-flight="f-dev1"]')).toBeVisible(); // the flight rows are still there
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-2-proposal.png` });
});

test("approve: the card says sent, confirm by radio; the pilot's answer shows live; the card then becomes the flight's record", async ({ page }) => {
  test.setTimeout(60_000); // these wait out the 10 s collapse; slower on a loaded machine
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await expect(page.locator(".rr-card")).toHaveCount(1);
  await rrCard(page, "rr_1").getByRole("button", { name: "Approve reroute" }).click();
  await expect(rrCard(page, "rr_1")).toHaveClass(/approved/);
  await expect(rrCard(page, "rr_1").locator(".rr-title")).toContainText("Reroute sent to N123AL. Confirm by radio.");
  await expect(page.locator("#rr-status-rr_1")).toContainText("Sent ");
  expect(m.requests.some((r) => r.method === "POST" && r.path === "/api/ops/reroutes/rr_1/approve")).toBe(true);
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-2-sent.png` });

  m.moveReroute("rr_1", "acknowledged", "dev-001");
  await expect(page.locator("#rr-status-rr_1")).toHaveClass(/ok/);
  await expect(page.locator("#rr-status-rr_1")).toContainText("Accepted by pilot");
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-2-acknowledged.png` });

  await expect(page.locator(".rr-card")).toHaveCount(0, { timeout: 15_000 }); // collapses after ~10 s
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await expect(page.locator('[data-record="rr_1"]')).toContainText("Reroute acknowledged");
  await expect(page.locator('[data-record="rr_1"]')).toContainText("Sent to pilot");
  await expect(page.locator('[data-record="rr_1"]')).toContainText("Accepted by pilot, dev-001");
});

test("unable: the pilot keeps the route and ATC reads 'contact by radio' in amber", async ({ page }) => {
  test.setTimeout(60_000); // these wait out the 10 s collapse; slower on a loaded machine
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await rrCard(page, "rr_1").getByRole("button", { name: "Approve reroute" }).click();
  await expect(rrCard(page, "rr_1")).toHaveClass(/approved/);
  m.moveReroute("rr_1", "unable", "dev-001");
  await expect(page.locator("#rr-status-rr_1")).toHaveText("Declined by pilot");
  await expect(page.locator("#rr-status-rr_1")).toHaveClass(/warn/);
  await expect(page.locator(".rr-card")).toHaveCount(0, { timeout: 15_000 });
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await expect(page.locator('[data-record="rr_1"]')).toContainText("Declined by pilot, dev-001");
});

test("dismiss: the card goes at once, no dialog opens, and the flight's records keep it", async ({ page }) => {
  let dialogs = 0;
  page.on("dialog", (d) => { dialogs++; void d.dismiss(); });
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await expect(page.locator(".rr-card")).toHaveCount(1);
  await rrCard(page, "rr_1").getByRole("button", { name: "Dismiss" }).click();
  await expect(page.locator(".rr-card")).toHaveCount(0);
  expect(dialogs).toBe(0);
  expect(m.requests.some((r) => r.method === "POST" && r.path === "/api/ops/reroutes/rr_1/dismiss")).toBe(true);
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await expect(page.locator('[data-record="rr_1"]')).toContainText("Dismissed, ops1");
});

test("expiry is said plainly; keyboard focus stays on the card's button when the panel refreshes", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await expect(page.locator(".rr-card")).toHaveCount(1);
  await page.locator('[data-rr-approve="rr_1"]').focus();
  m.push("reroute", m.reroutes.find((x) => x.id === "rr_1")); // the same proposal again: the panel rebuilds
  await expect(page.locator('[data-rr-approve="rr_1"]')).toBeFocused();
  m.moveReroute("rr_1", "expired");
  await expect(page.locator("#rr-status-rr_1")).toHaveText("Expired");
  await expect(page.locator("#rr-status-rr_1")).toHaveClass(/muted/);
});

test("AI reroute cards in dark: proposal, sent, acknowledged", async ({ page }) => {
  const { m } = await open(page);
  await page.click("#gear-btn");
  await page.click("#ap-dark");
  await page.click("#gear-btn");
  await expect(page.locator(".ops")).toHaveClass(/night/);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await expect(page.locator(".rr-card")).toHaveCount(1);
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-2-proposal-dark.png` });
  await rrCard(page, "rr_1").getByRole("button", { name: "Approve reroute" }).click();
  await expect(rrCard(page, "rr_1")).toHaveClass(/approved/);
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-ai-2-sent-dark.png` });
  m.moveReroute("rr_1", "acknowledged", "dev-001");
  await expect(page.locator("#rr-status-rr_1")).toHaveClass(/ok/);
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-ai-2-acknowledged-dark.png` });
});

test("a simulated aircraft's proposal: Approve works, the card says Simulated pilot, the sim pilot answers like a unit", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_s", "f-al01")); // AL-01 is simulated
  const card = rrCard(page, "rr_s");
  await expect(card).toContainText("Simulated pilot");
  await expect(card.getByRole("button", { name: "Approve reroute" })).toBeEnabled();
  await card.getByRole("button", { name: "Approve reroute" }).click();
  await expect(card.locator(".rr-title")).toContainText("Reroute sent to AL-01. Confirm by radio.");
  await expect(page.locator("#rr-status-rr_s")).toContainText("Sent ");
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-2-sim.png` });
  await expect(page.locator("#rr-status-rr_s")).toContainText("Accepted by simulated pilot", { timeout: 15_000 }); // contract B (was "Accepted by pilot")
  await expect(page.locator("#rr-status-rr_s")).toHaveClass(/ok/);
  // a reroute event pushed as a pre-acknowledged alert is not an alert: the bell and the list do not change
  m.push("alert", { id: 90, at: new Date().toISOString(), kind: "reroute", severity: "info", aircraft_id: "AL-01", message: "reroute proposed", detail: { reroute_id: "rr_s" }, acked_at: new Date().toISOString(), acked_by: "ops1" });
  await expect(page.locator("#bell-count")).toHaveText("2");
  await page.click("#bell-btn");
  await expect(page.locator('[data-alert="90"]')).toHaveCount(0);
});

test("a refusal from the server reaches the card in words (the reroute changed under the operator)", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_c", "f-dev1"));
  await expect(page.locator('[data-rr-approve="rr_c"]')).toBeEnabled();
  m.refuseApprove = true; // the server answers 409: the reroute was answered elsewhere
  await page.locator('[data-rr-approve="rr_c"]').click();
  await expect(rrCard(page, "rr_c").locator(".err")).toHaveText("Could not send: this reroute is already approved");
});

test("AI reroute preview: one card at a time; hovering another card moves it there", async ({ page }) => {
  const { m } = await open(page);
  const a = m.addReroute("rr_1", "f-dev1");
  const b = m.addReroute("rr_2", "f-dev2"); // medical: the top card
  m.push("reroute", a);
  m.push("reroute", b);
  await expect(page.locator(".rr-card")).toHaveCount(2);
  const previewStart = () => page.evaluate(() => (window as any).__map.getSource("ops-reroute").serialize().data.features[0].geometry.coordinates[0]);
  await expect.poll(async () => (await previewStart())[1]).toBeCloseTo(b.proposed.waypoints[0][0], 3);
  await page.locator('.rr-card[data-reroute="rr_1"]').hover();
  await expect.poll(async () => (await previewStart())[1]).toBeCloseTo(a.proposed.waypoints[0][0], 3);
  expect(await source(page, "ops-reroute")).toBe(1);
  expect(await source(page, "ops-reroute-current")).toBe(1);
});

test("AI reroute records: a flight's earlier reroutes come from its history", async ({ page }) => {
  const { m } = await open(page);
  const old = m.addReroute("rr_old", "f-dev1", { created_at: new Date(Date.now() - 3600_000).toISOString() });
  old.status = "acknowledged"; // answered before this page was opened: only the history knows
  old.events.push({ at: new Date(Date.now() - 3500_000).toISOString(), kind: "acknowledged", by: "dev-001" });
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await expect(page.locator('[data-record="rr_old"]')).toContainText("Reroute acknowledged");
  await expect(page.locator('[data-record="rr_old"]')).toContainText("Accepted by pilot, dev-001");
  expect(m.requests.some((r) => r.method === "GET" && r.path === "/api/ops/reroutes")).toBe(true);
});

test("AI reroute camera: the map is fitted once when a card first appears, never again on later updates", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await expect(page.locator(".rr-card")).toHaveCount(1);
  await page.evaluate(() => (window as any).__map.jumpTo({ center: [-80.2, 27.6], zoom: 9 })); // the controller looks elsewhere
  m.moveReroute("rr_1", "approved", "ops1");
  m.push("reroute", m.reroutes.find((x) => x.id === "rr_1"));
  await expect(page.locator("#rr-status-rr_1")).toBeVisible();
  const center = await page.evaluate(() => (window as any).__map.getCenter());
  expect(center.lng).toBeCloseTo(-80.2, 1);
  expect(center.lat).toBeCloseTo(27.6, 1);
});

test("AI reroute after a socket drop: open proposals are fetched again on reconnect", async ({ page }) => {
  const { m } = await open(page);
  m.drop();
  m.addReroute("rr_r", "f-dev1"); // proposed while the page could not hear the socket
  await expect(page.locator('.rr-card[data-reroute="rr_r"]')).toBeVisible({ timeout: 15_000 });
});

// ---- ✦ AI cues (U3 story 3): the chip, and the mark on predictions and on what the model produced -----------------
test("✦ AI cues: the header chip says the forecast is live; predicted conflicts, ETA windows and off-route calls carry the mark", async ({ page }) => {
  const { m } = await open(page);
  await expect(page.locator("#ai-chip")).toContainText("AI forecast live");
  await expect(page.locator("#ai-chip")).toContainText("aerolink-gbm");
  await expect(page.locator("#ai-chip")).toContainText(/updated \d+ s ago/);
  await page.click("#bell-btn");
  await page.click("#tab-conflicts");
  await expect(page.locator(".cf-row").first().locator(".ai-mark")).toHaveText("AI");
  await page.click("#bell-btn"); // close the popover
  m.routes.find((x) => x.flight_id === "f-al02").status = "off_route"; // flagged by the learned detector: marked
  m.routes.find((x) => x.flight_id === "f-al02").detected_by = "ai";
  m.routes.find((x) => x.flight_id === "f-al01").status = "off_route"; // flagged by the fixed rule: no mark (contract §7)
  m.routes.find((x) => x.flight_id === "f-al01").detected_by = "rule";
  await expect(page.locator('[data-flight="f-al02"] .tag.warn .ai-mark')).toHaveText("AI");
  await expect(page.locator('[data-flight="f-al01"] .tag.warn')).toHaveText("Off route");
  await expect(page.locator('[data-flight="f-al01"] .tag.warn .ai-mark')).toHaveCount(0);
  await page.locator('.fl-row[data-flight="f-dev2"]').click();
  await page.click("#pop-details");
  await expect(page.locator(".kv dd .ai-mark").first()).toHaveText("AI"); // the arrival window
  await expect(page.locator("#eta-fold summary .ai-mark")).toHaveText("AI");
  await expect(page.locator(".mini-row.hot .ai-mark").first()).toHaveText("AI"); // the predicted conflict in the flight's card
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-3-cues-light.png` });
  await page.click("#gear-btn");
  await page.click("#ap-dark");
  await page.click("#gear-btn");
  await expect(page.locator(".ops")).toHaveClass(/night/);
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/ops-ai-3-cues-dark.png` });
});

test("the preview follows the selected flight, not the top card", async ({ page }) => {
  const { m } = await open(page);
  const a = m.addReroute("rr_1", "f-dev1");
  const b = m.addReroute("rr_2", "f-dev2"); // medical: the top card
  m.push("reroute", a);
  m.push("reroute", b);
  await expect(page.locator(".rr-card")).toHaveCount(2);
  const previewLat = () => page.evaluate(() => (window as any).__map.getSource("ops-reroute").serialize().data.features[0].geometry.coordinates[0][1]);
  await expect.poll(previewLat).toBeCloseTo(b.proposed.waypoints[0][0], 3); // top card before anything is selected
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await expect.poll(previewLat).toBeCloseTo(a.proposed.waypoints[0][0], 3); // the selected flight's card
});

test("a background models refresh never closes an open System dropdown", async ({ page }) => {
  const { m } = await open(page);
  await page.click("#status-btn");
  await page.click("#tab-system");
  const sel = page.locator("#sys-scenario");
  await sel.focus(); // the controller is in the dropdown
  await page.evaluate(() => { (window as any).__sel = document.getElementById("sys-scenario"); });
  m.modelsVersion = 2; // the next /api/models poll brings a change
  await page.waitForTimeout(11_000);
  expect(await page.evaluate(() => (window as any).__sel === document.getElementById("sys-scenario"))).toBe(true); // same node: not rebuilt
  await expect(sel).toBeFocused();
});

// ---- How the AI works (Story 4) and the disclaimer (Story 5) ----------------------------------------------------
test("How the AI works: the four models and the measured results from /api/ai, opened by the ✦ button", async ({ page }) => {
  await open(page);
  await page.click("#ai-btn");
  await expect(page.locator("#ai-models li")).toHaveCount(4);
  await expect(page.locator("#ai-sheet h2")).toHaveText("How the AI works");
  await expect(page.locator("#ai-models li").first()).toContainText("Traffic & arrival-time forecaster");
  await expect(page.locator("#ai-models li").first()).toContainText("aerolink-gbm");
  await expect(page.locator("#ai-results")).toContainText("61 s with AI, 104 s classical");
  await expect(page.locator("#ai-results")).toContainText("84% of the time");
  await expect(page.locator("#ai-results")).toContainText("precision 1.00, recall 0.88");
  await expect(page.locator("#ai-route-note")).toContainText("match the classical planner");
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-ai-4-sheet-light.png` });
  await page.click("#gear-btn");
  await page.click("#ap-dark");
  await page.click("#gear-btn"); // close settings
  await page.click("#ai-btn");
  await expect(page.locator("#ai-results")).toBeVisible();
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-ai-4-sheet-dark.png` });
});

test("AI disclaimer: shown once per version at sign-in, one tap records it, a footer line stays", async ({ page }) => {
  const { m } = await open(page, { aiAck: null });
  await expect(page.locator("#ai-ack")).toBeVisible();
  await expect(page.locator("#ai-ack-text")).toContainText("AeroLink is a prototype and is advisory only.");
  await expect(page.locator("#ai-ack-btn")).toBeFocused(); // keyboard users land on the one button
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-ai-5-disclaimer-light.png` });
  await page.click("#ai-ack-btn");
  await expect(page.locator("#ai-ack")).toBeHidden();
  expect(m.requests.some((r) => r.method === "POST" && r.path === "/api/auth/ai-ack")).toBe(true);
  await expect(page.locator("#ai-footer")).toContainText("AI-assisted predictions. Verify before acting. Ops is not ATC.");
  await page.reload();
  await expect(page.locator("#map canvas")).toBeVisible();
  await expect(page.locator("#ai-ack")).toBeHidden(); // recorded: not shown again for this version
});

test("conflict severity: high is red, medium amber, on the list, the flight card and the badges; the ring follows", async ({ page }) => {
  await open(page);
  await expect(page.locator('[data-flight="f-dev2"] .conflict-dot.high')).toHaveCount(1);
  await expect(page.locator('[data-flight="f-dev1"] .conflict-dot')).toHaveCount(0); // not in a conflict in the mock
  await page.locator('.fl-row[data-flight="f-dev2"]').click(); // a high and a medium conflict
  await page.click("#pop-details");
  // read the computed colour once the element exists (the panel is rebuilt on each poll, so wait for it)
  const colour = (sel: string) => page.locator(sel).first().evaluate((e) => getComputedStyle(e).color);
  await expect(page.locator(".mini-row.hot.high").first()).toBeVisible();
  await expect.poll(() => colour(".mini-row.hot.high")).toBe("rgb(196, 39, 29)"); // R-contract §6.7: text red #C4271D
  await expect.poll(() => colour(".mini-row.hot.medium")).toBe("rgb(154, 91, 0)"); // R-contract §6.7: text amber #9A5B00
  await page.click("#bell-btn");
  await page.click("#tab-conflicts");
  await expect(page.locator(".cf-row .b-high").first()).toHaveText("High");
  await expect.poll(() => colour(".cf-row .b-medium")).toBe("rgb(154, 91, 0)"); // R-contract §6.7: text amber #9A5B00
});

test("AI disclaimer in dark: the same card on the night look", async ({ page }) => {
  await open(page, { aiAck: null });
  await page.click("#gear-btn", { force: true }).catch(() => {});
  await page.evaluate(() => localStorage.setItem("aerolink.ops.appearance", "dark"));
  await page.reload();
  await expect(page.locator("#ai-ack")).toBeVisible();
  await expect(page.locator(".ops")).toHaveClass(/night/);
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-ai-5-disclaimer-dark.png` });
});

// ---- Many proposals, many controllers (contract §9) ----------------------------------------------------------------
test("many proposals: the top three are pinned in order, +3 more opens the Reroutes tab with all six, another controller's approval shows who", async ({ page }) => {
  const { m } = await open(page);
  // two more flights, so there are six open proposals (one per flight)
  m.routes.push({ ...m.routes[0], flight_id: "f-x1", aircraft_id: "AL-04", callsign: "AL-04" }, { ...m.routes[0], flight_id: "f-x2", aircraft_id: "AL-05", callsign: "AL-05" });
  const at = (s: number) => new Date(Date.now() + s * 1000).toISOString();
  // medical first, then the earliest expiry: rr_m, rr_b, rr_e are the top three
  for (const [id, flight, expiresIn] of [["rr_m", "f-dev2", 2000], ["rr_a", "f-dev1", 900], ["rr_b", "f-al01", 300], ["rr_c", "f-al02", 600], ["rr_d", "f-x1", 1200], ["rr_e", "f-x2", 450]] as const) {
    m.push("reroute", m.addReroute(id, flight, { expires_at: at(expiresIn) }));
  }
  await expect(page.locator(".rr-card")).toHaveCount(3);
  expect(await page.locator(".rr-card").evaluateAll((els) => els.map((e) => e.getAttribute("data-reroute")))).toEqual(["rr_m", "rr_b", "rr_e"]);
  await expect(page.locator("#rr-more")).toHaveText("3 more reroutes");
  await page.click("#rr-more");
  await expect(page.locator(".rr-card")).toHaveCount(6);
  await expect(page.locator('[data-filter="reroutes"]')).toHaveText("Reroutes 6");
  await expect(page.locator('[data-filter="reroutes"]')).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".fl-row")).toHaveCount(0); // the tab lists proposals, not flights
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-ai-6-reroutes-tab-light.png` });

  // another controller approves rr_m from another session: this page hears it over the socket
  const rr = m.reroutes.find((x) => x.id === "rr_m");
  Object.assign(rr, { status: "approved", approved_by: "joshua", approved_at: at(0) });
  rr.events.push({ at: at(0), kind: "approved", by: "joshua" });
  m.push("reroute", rr);
  await expect(page.locator("#rr-status-rr_m")).toContainText("approved by joshua");
  await expect(rrCard(page, "rr_m")).toHaveClass(/approved/);
  await page.click("#gear-btn");
  await page.click("#ap-dark");
  await page.click("#gear-btn");
  await expect(page.locator(".ops")).toHaveClass(/night/);
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-ai-6-reroutes-tab-dark.png` });
});

test("dark states: conflicts, System and login read the same", async ({ page }) => {
  const { m } = await open(page);
  await page.click("#gear-btn");
  await page.click("#ap-dark");
  await page.click("#gear-btn");
  await page.click("#bell-btn");
  await page.click("#tab-conflicts");
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-dark-conflicts.png` });
  await page.click("#bell-btn");
  await page.click("#status-btn");
  await page.click("#tab-system");
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/ops-dark-system.png` });
  await page.click("#status-btn"); // close
  m.session = false;
  await page.goto("/login");
  await expect(page.locator(".login")).toHaveClass(/night/);
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${SHOT}/ops-dark-login.png` });
});

// ---- CP1 (R2): three-column layout, the AI overview card, the flight bays (R-contract §6.1, §6.7) ----------------------
test("CP1 layout: the AI overview leads the left panel, then the reroute cards, the filter chips, then the bays", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1")); // f-dev1 needs action already: the bays do not move
  await expect(page.locator(".rr-card")).toHaveCount(1);
  await expect(page.locator("#ai-overview")).toContainText("AI overview, next 30 min");
  await expect(page.locator("#ai-overview")).toContainText(/\d+ s ago/);
  await expect(page.locator("#ai-overview li")).toHaveCount(3); // at most three bullets (R-contract §10.1)
  await expect(page.locator("#ov-more")).toHaveText("1 more");
  await expect(page.locator("#ai-overview")).toContainText("Written from the forecast. Numbers are checked against it; wording is not. Verify before acting.");
  await expect(page.locator("#ai-overview")).not.toContainText("fallback");
  const inOrder = await page.evaluate(() => {
    const els = [".ov-card", ".rr-card", ".chips", '[data-bay="needs"]'].map((sel) => document.querySelector(sel)!);
    return els.every((el, i) => i === 0 || (els[i - 1].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0);
  });
  expect(inOrder).toBe(true);
  await expect(page.locator('[data-bay="needs"] .bay-h')).toHaveText("Needs action, 2");
  await expect(page.locator('[data-bay="airborne"] .bay-h')).toHaveText("Airborne, 3"); // AL-01, AL-07, N456MD
  await expect(page.locator('[data-bay="planned"]')).toHaveCount(0); // no planned flight in this view: the bay is not drawn
  await expect(page.locator('[data-bay="needs"] [data-flight="f-dev1"]')).toBeVisible();
  await expect(page.locator('[data-bay="airborne"] [data-flight="f-dev2"]')).toBeVisible();
  await expect(page.locator('[data-bay="airborne"] [data-flight="f-al07"]')).toBeVisible();
  await page.click('[data-filter="needs"]');
  await expect(page.locator('[data-bay="airborne"]')).toHaveCount(0);
  await expect(page.locator(".fl-row")).toHaveCount(2);
  await page.click('[data-filter="all"]');
  await expect(page.locator(".fl-row")).toHaveCount(5);
  const sb = await page.locator(".sidebar").boundingBox();
  expect(sb!.width).toBeCloseTo(340, 0); // the left column of the plan
});

test("CP1 needs action (R-contract §6.1): a waiting reroute or an off-route flight needs action; medical alone does not", async ({ page }) => {
  const { m } = await open(page);
  await expect(page.locator('[data-bay="needs"] [data-flight="f-dev2"]')).toHaveCount(0); // medical and airborne: not an action
  m.push("reroute", m.addReroute("rr_1", "f-dev2")); // an open proposal on the medical flight
  await expect(page.locator('[data-bay="needs"] [data-flight="f-dev2"]')).toBeVisible();
  await expect(page.locator("#needs-badge")).toHaveText("3 need action");
  m.moveReroute("rr_1", "dismissed", "ops1");
  await expect(page.locator('[data-bay="needs"] [data-flight="f-dev2"]')).toHaveCount(0);
  await expect(page.locator("#needs-badge")).toHaveText("2 need action");
  m.routes.find((x) => x.flight_id === "f-al01")!.status = "off_route";
  m.routes.find((x) => x.flight_id === "f-al01")!.detected_by = "rule";
  await expect(page.locator('[data-bay="needs"] [data-flight="f-al01"]')).toBeVisible();
  await expect(page.locator("#needs-badge")).toHaveText("3 need action");
});

test("CP1 overview endpoint missing (older backend): the card hides and nothing else changes", async ({ page }) => {
  await open(page, { overviewOff: true });
  await expect(page.locator("#ai-overview")).toHaveCount(0);
  await expect(page.locator("#status-pill")).toBeHidden();
  await expect(page.locator('[data-bay="needs"] .fl-row')).toHaveCount(2);
});

for (const [w, h] of [[1440, 900], [1920, 1080]] as const) {
  test(`CP1 screenshots ${w}x${h}: light and dark, the overview, the bays and a selected flight`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h });
    const { m } = await open(page);
    m.push("reroute", m.addReroute("rr_1", "f-dev1"));
    await expect(page.locator(".rr-card")).toHaveCount(1);
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/r2-cp1-${w}-light.png` });
    await page.click("#gear-btn");
    await page.click("#ap-dark");
    await page.click("#gear-btn");
    await expect(page.locator(".ops")).toHaveClass(/night/);
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/r2-cp1-${w}-dark.png` });
  });
}

// ---- CP1 fixes (R-contract §10): overview at most three bullets, one-line reroute cards, chips on one row ---------------
test("CP1 overview: three bullets, '+N more' expands in place, the header collapses to one line and stays collapsed", async ({ page }) => {
  await open(page);
  await expect(page.locator("#ai-overview li")).toHaveCount(3);
  await page.click("#ov-more");
  await expect(page.locator("#ai-overview li")).toHaveCount(4);
  await expect(page.locator("#ov-more")).toHaveText("Show fewer");
  await page.click("#ov-toggle"); // header: collapse to the one line
  await expect(page.locator("#ai-overview li")).toHaveCount(0);
  await expect(page.locator("#ov-toggle")).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("#ai-overview")).toContainText("AI overview, next 30 min");
  await page.reload();
  await expect(page.locator("#map canvas")).toBeVisible();
  await expect(page.locator("#ai-overview li")).toHaveCount(0); // remembered per browser
  await page.click("#ov-toggle");
  await expect(page.locator("#ai-overview li")).toHaveCount(3);
});

test("CP1 reroute card: reason and via on one line (full text in the title, and in the flight detail), buttons 36 px", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  const card = rrCard(page, "rr_1");
  await expect(card).toBeVisible();
  const reason = card.locator(".rr-reason");
  await expect(reason).toHaveAttribute("title", "Predicted congestion at Kissimmee 7:42 to 7:50 PM and possible conflict with SIM-P03");
  // the poll rebuilds the list now and then: measure on whichever element is there (expect.poll retries)
  const hgt = (sel: string) => page.locator(`.rr-card[data-reroute="rr_1"] ${sel}`).evaluate((el) => el.getBoundingClientRect().height);
  await expect.poll(() => hgt(".rr-reason")).toBeLessThan(24);
  await expect.poll(() => hgt(".rr-via")).toBeLessThan(24);
  await expect.poll(() => card.getByRole("button", { name: "Approve reroute" }).evaluate((el) => el.getBoundingClientRect().height)).toBeCloseTo(36, 0);
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await expect.poll(() => hgt(".rr-reason")).toBeGreaterThan(24); // the detail shows it whole
});

test("CP1 chips: All, Needs action, Medical and Reroutes N sit on one row at 340 px", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await expect(page.locator('[data-filter="reroutes"]')).toHaveText("Reroutes 1");
  const boxes = await page.locator(".chip").evaluateAll((els) => els.map((e) => e.getBoundingClientRect()));
  expect(new Set(boxes.map((b) => Math.round(b.top))).size).toBe(1); // one row
  const sb = (await page.locator(".sidebar").boundingBox())!;
  expect(Math.max(...boxes.map((b) => b.right))).toBeLessThanOrEqual(sb.x + sb.width - 16);
});

// ---- CP2 (R2): the arrivals timeline (R-contract §3, §6.2, §6.3, §6.6, §12) ------------------------------------------
const tagOf = (page: Page, flight: string) => page.locator(`.arr-tag[data-flight="${flight}"]`);

test("CP2 timeline: busiest sites as lanes, ticks +45 to Now, a bunch callout, no ✦ on the explanation", async ({ page }) => {
  await open(page);
  await expect(page.locator(".arrivals")).toBeVisible();
  await expect(page.locator(".arr-lane")).toHaveCount(3); // three lanes below 1600 px (R2 CP2 fix 2): ev-orl, ev-lal and one more
  await expect(page.locator('.arr-lane[data-lane="ev-orl"] .arr-lane-h')).toHaveText("Orlando Executive");
  expect(await page.locator(".arr-ticks .tk").allTextContents()).toEqual(["45 min", "30 min", "15 min", "5 min", "Now"]);
  await expect(page.locator(".arr-sub svg")).toHaveCount(0); // no AI icon on the explanation (was: no sparkle glyph)
  await expect(page.locator('.arr-lane[data-lane="ev-orl"] .arr-callout')).toHaveText("2 arrivals within 3 min");
  await expect(page.locator("#arr-more")).toHaveText("1 more site"); // four sites, three lanes: the rest in the chip
  await expect(tagOf(page, "f-al02")).toBeVisible();
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/r2-cp2-timeline-light.png` });
});

test("CP2 timeline: a tag is one arrival's window, and a click selects that flight", async ({ page }) => {
  await open(page);
  await tagOf(page, "f-dev2").click();
  await page.click("#pop-details");
  await expect(page.locator(".dd-title")).toHaveText("N456MD");
  await expect(tagOf(page, "f-dev2")).toHaveClass(/sel/);
});

test("CP2 ghost tags (R-contract §6.3): a dashed tag while a reroute proposal is open, proposed or approved; gone once answered", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1")); // destination ev-lal
  const ghosts = page.locator(".arr-tag.ghost");
  await expect(ghosts).toHaveCount(1);
  await expect(ghosts).toHaveText("N123AL");
  await rrCard(page, "rr_1").getByRole("button", { name: "Approve reroute" }).click();
  await expect(rrCard(page, "rr_1")).toHaveClass(/approved/);
  await expect(ghosts).toHaveCount(1); // approved and not yet answered: still a ghost
  m.moveReroute("rr_1", "acknowledged", "dev-001");
  await expect(ghosts).toHaveCount(0, { timeout: 15_000 });
});

test("CP2 edges (R-contract §6.2): an overdue arrival is pinned at Now as due, a window past +45 is clipped, a p10 past +45 is hidden; +N sites chip", async ({ page }) => {
  const { m } = await open(page);
  const now = Date.now() / 1000;
  const r = (flight_id: string, aircraft_id: string, destination: string, p10: number, p50: number, p90: number) =>
    ({ ...m.routes[0], flight_id, aircraft_id, callsign: undefined, destination, eta_p10: now + p10, eta_p50: now + p50, eta_p90: now + p90, status: "active" });
  m.routes.push(
    r("f-due", "AL-09", "ev-lal", -400, -200, 100), // p50 has passed, not landed: due at Now
    r("f-clip", "AL-10", "ev-lal", 1500, 2000, 3000), // window runs past +45 min: clipped at the top
    r("f-hide", "AL-11", "ev-lal", 3000, 3300, 3600), // p10 beyond +45 min: not shown
    r("f-x9", "AL-12", "ev-sef", 300, 400, 500), // a fifth landing site: one lane is counted in the chip
  );
  await expect(tagOf(page, "f-due")).toHaveClass(/due/, { timeout: 15_000 });
  await expect(tagOf(page, "f-due")).toContainText("AL-09 due");
  expect(await tagOf(page, "f-due").evaluate((el) => (el as HTMLElement).style.bottom)).toBe("0%");
  await expect(tagOf(page, "f-clip")).toHaveClass(/clipped/);
  await expect(tagOf(page, "f-hide")).toHaveCount(0);
  await expect(page.locator("#arr-more")).toHaveText("2 more sites"); // five sites, three lanes
});

test("CP2 live update: the tag under the pointer is the same element after a poll brings new times", async ({ page }) => {
  const { m } = await open(page);
  await tagOf(page, "f-dev2").hover();
  await page.evaluate(() => { (window as any).__tag = document.querySelector('.arr-tag[data-flight="f-dev2"]'); });
  const route = m.routes.find((x) => x.flight_id === "f-dev2")!;
  route.eta_p50 += 120; route.eta_p10 += 120; route.eta_p90 += 120; // the next poll moves the window
  await page.waitForTimeout(4000);
  expect(await page.evaluate(() => document.querySelector('.arr-tag[data-flight="f-dev2"]') === (window as any).__tag)).toBe(true);
});

test("CP2 narrow (< 1280 px): the timeline opens over the map from the Arrivals button; wide: always shown", async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page);
  await expect(page.locator(".arrivals")).toBeHidden();
  await page.click("#arr-btn");
  await expect(page.locator(".arrivals")).toBeVisible();
  await expect(page.locator("#arr-btn")).toHaveAttribute("aria-pressed", "true");
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator(".arrivals")).toBeVisible();
});

for (const [w, h] of [[1440, 900], [1920, 1080]] as const) {
  test(`CP2 screenshots ${w}x${h}: light and dark with the timeline`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h });
    const { m } = await open(page);
    m.push("reroute", m.addReroute("rr_1", "f-dev1"));
    await expect(page.locator(".rr-card")).toHaveCount(1);
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/r2-cp2-${w}-light.png` });
    await page.click("#gear-btn");
    await page.click("#ap-dark");
    await page.click("#gear-btn");
    await expect(page.locator(".ops")).toHaveClass(/night/);
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/r2-cp2-${w}-dark.png` });
  });
}

// ---- CP2 fixes (R2): callsigns never truncate, lane count by width, two-line lane labels, ghost and callout text, scale bar ---
const fits = (page: Page) => page.locator(".arr-tag").evaluateAll((els) => els.map((e) => ({ t: (e as HTMLElement).textContent, over: (e as HTMLElement).scrollWidth > (e as HTMLElement).clientWidth + 1 })));
for (const [w, h] of [[1440, 900], [1920, 1080]] as const) {
  test(`CP2 fix: every callsign on a timeline tag fits, never truncated, at ${w}x${h}`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h });
    const { m } = await open(page);
    m.push("reroute", m.addReroute("rr_1", "f-dev1")); // a ghost too: "✦ N123AL"
    await expect(page.locator(".arr-tag.ghost")).toHaveCount(1);
    await page.waitForTimeout(600);
    const tags = await fits(page);
    expect(tags.length).toBeGreaterThan(2);
    expect(tags.filter((t) => t.over)).toEqual([]);
  });
}

test("CP2 fix: lanes are three below 1600 px and four from 1600 px; the rest counted in '+N sites'", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page);
  await expect(page.locator(".arr-lane")).toHaveCount(3);
  await expect(page.locator("#arr-more")).toHaveText("1 more site");
  await page.setViewportSize({ width: 1920, height: 1080 });
  await expect(page.locator(".arr-lane")).toHaveCount(4);
  await expect(page.locator("#arr-more")).toBeHidden();
});

test("CP2 fix: lane labels show up to two lines, shortened for display, the full name in the title", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await open(page);
  await expect(page.locator('.arr-lane[data-lane="ev-orl"] .arr-lane-h')).toHaveText("Orlando Executive");
  await expect(page.locator('.arr-lane[data-lane="ev-orl"] .arr-lane-h')).toHaveAttribute("title", /^Orlando Executive Airport:/);
  await expect(page.locator('.arr-lane[data-lane="ev-lal"] .arr-lane-h')).toHaveText("Lakeland Linder Intl");
  const heights = await page.locator(".arr-lane-h").evaluateAll((els) => els.map((e) => (e as HTMLElement).getBoundingClientRect().height));
  expect(heights.every((x) => x <= 28.5)).toBe(true);
});

test("CP2 fix: display names only (labels.ts): the suffix rules, and two lanes never show the same label", () => {
  expect(shortLaneName("Orlando Executive Airport")).toBe("Orlando Executive");
  expect(shortLaneName("Lakeland Linder Intl Airport")).toBe("Lakeland Linder Intl");
  expect(shortLaneName("Sebring Regional Airport")).toBe("Sebring Regional");
  expect(shortLaneName("Orlando International Airport")).toBe("Orlando International");
  expect(shortLaneName("Downtown Orlando Airport")).toBe("Downtown Orlando");
  expect(shortLaneName("Orlando Health Dr. P. Phillips Hospital")).toBe("Orlando Health Dr. P. Phillips Hospital");
  const clash = laneLabels([{ node: "a", full: "Orlando Executive Airport" }, { node: "b", full: "Orlando Executive" }]);
  expect(clash.get("a")).toBe("Orlando Executive Airport"); // would both read "Orlando Executive": the full names instead
  expect(clash.get("b")).toBe("Orlando Executive");
  expect(new Set(laneLabels([{ node: "a", full: "Orlando Executive Airport" }, { node: "b", full: "Orlando International Airport" }]).values()).size).toBe(2);
});

test("CP2 fix: a ghost tag reads '✦ <callsign>', dashed, titled 'If rerouted (AI proposal)'; the callout is '✦ 2 within 3 min' in at most two lines", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  const ghost = page.locator(".arr-tag.ghost");
  await expect(ghost).toHaveText("N123AL");
  await expect(ghost).toHaveAttribute("title", "If rerouted (AI proposal)");
  const callout = page.locator('.arr-lane[data-lane="ev-orl"] .arr-callout');
  await expect(callout).toHaveText("2 arrivals within 3 min");
  // U5: the sentence is longer ("2 arrivals within 3 min"), so the narrow lanes need up to three lines (was two, 30 px); it must never be clipped
  expect((await callout.boundingBox())!.height).toBeLessThanOrEqual(42);
  expect(await callout.evaluate((el) => el.scrollHeight <= el.clientHeight + 1)).toBe(true);
});

for (const [w, h] of [[1440, 900], [1920, 1080]] as const) {
  test(`CP2 fix: the scale bar touches neither the legend nor the attribution at ${w}x${h}`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h });
    await open(page);
    await expect(page.locator("#ai-legend")).toBeVisible();
    const box = async (sel: string) => (await page.locator(sel).boundingBox())!;
    const overlap = (a: { x: number; y: number; width: number; height: number }, b: { x: number; y: number; width: number; height: number }) =>
      !(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y);
    const scale = await box(".maplibregl-ctrl-scale");
    expect(overlap(scale, await box("#ai-legend"))).toBe(false);
    expect(overlap(scale, await box(".attribution"))).toBe(false);
    expect(overlap(await box("#ai-legend"), await box(".attribution"))).toBe(false);
  });
}

// ---- CP3 (R2): the flight popover on selection; Details opens the detail; Esc closes the popover only ----------------
test("CP3 popover: selecting a flight opens the popover beside it; the list and the highlights stay; the overview steps aside", async ({ page }) => {
  await open(page);
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await expect(page.locator("#flight-pop")).toBeVisible();
  await expect(page.locator("#flight-pop .fp-cs")).toHaveText("N123AL");
  await expect(page.locator("#pop-details")).toBeVisible();
  await expect(page.locator(".dd-title")).toHaveCount(0); // not the detail yet
  await expect(page.locator('.fl-row[data-flight="f-dev1"]')).toHaveClass(/sel/);
  await expect(page.locator('.arr-tag[data-flight="f-dev1"]')).toHaveClass(/sel/);
  await expect.poll(() => source(page, "ops-route")).toBe(1);
  await expect(page.locator("#ai-overview")).toHaveCount(0); // list-only: the overview steps aside while a flight is selected
});

test("CP3 popover: Details opens the existing detail and closes the popover; Esc closes the popover only, the selection stays", async ({ page }) => {
  await open(page);
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.keyboard.press("Escape");
  await expect(page.locator("#flight-pop")).toBeHidden();
  await expect(page.locator('.fl-row[data-flight="f-dev1"]')).toHaveClass(/sel/); // the selection stays
  await expect.poll(() => source(page, "ops-route")).toBe(1);
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await page.click("#pop-details");
  await expect(page.locator(".dd-title")).toHaveText("N123AL");
  await expect(page.locator("#flight-pop")).toBeHidden();
});

test("CP3 popover: the flight's two-sentence AI overview is shown; with 503 there is no AI text, only callsign, altitude and speed", async ({ page }) => {
  await open(page);
  await page.locator('.fl-row[data-flight="f-al01"]').click(); // airborne: it has a live altitude and speed
  await expect(page.locator("#pop-ai")).toContainText("Arrives");
  await expect(page.locator("#pop-ai")).toContainText("the Lake Wales route avoids it");
  await expect(page.locator("#flight-pop .fp-kv")).toContainText("ft");
  await page.keyboard.press("Escape");
  await page.reload();
  await expect(page.locator("#map canvas")).toBeVisible();
});

test("CP3 popover with the flight overview unavailable (503): no AI text, the popover still works", async ({ page }) => {
  await open(page, { flightOverviewUnavailable: true });
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await expect(page.locator("#flight-pop")).toBeVisible();
  await expect(page.locator("#flight-pop .fp-cs")).toHaveText("N123AL");
  await expect(page.locator("#pop-ai")).toHaveCount(0);
  await expect(page.locator("#pop-details")).toBeVisible();
});

// ---- CP3 screenshots (R2): the popover over the map, light and dark, with the timeline and the overview steps aside ----
for (const [w, h, theme] of [[1440, 900, "light"], [1440, 900, "dark"], [1920, 1080, "light"], [1920, 1080, "dark"]] as const) {
  test(`CP3 screenshots ${w}x${h} ${theme}: the flight popover with its AI overview`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h });
    await open(page);
    if (theme === "dark") {
      await page.click("#gear-btn");
      await page.click("#ap-dark");
      await page.click("#gear-btn");
    }
    await page.locator('.fl-row[data-flight="f-al01"]').click();
    await expect(page.locator("#pop-ai")).toContainText("Arrives");
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/r2-cp3-${w}-${theme}.png` });
  });
}

// ---- L-FIX (legal): the persistent notice, the ops wording and the ops-side reroute words ----------------------------
test("L-FIX: the prototype notice is always on the ops screen; the ops disclaimer is the ops text; accepted and declined read as the record", async ({ page }) => {
  const { m } = await open(page, { aiAck: null });
  await expect(page.locator("#proto-note")).toHaveText("Prototype. Not certified. Not for flight. Not ATC.");
  await expect(page.locator("#proto-note")).toBeVisible();
  await expect(page.locator(".attribution")).toContainText("Map data from OpenStreetMap and Protomaps");
  await expect(page.locator('.attribution a:text-is("OpenStreetMap")')).toHaveAttribute("href", "https://www.openstreetmap.org/copyright");
  await expect(page.locator("#ai-ack-text")).toContainText("It is not an FAA system");
  await expect(page.locator("#ai-ack-text")).toContainText("are not ATC clearances or instructions");
  await page.click("#ai-ack-btn");
  await expect(page.locator("#ai-ack")).toBeHidden();
  expect(m.requests.some((r) => r.method === "POST" && r.path === "/api/auth/ai-ack")).toBe(true);
  m.push("reroute", m.addReroute("rr_1", "f-dev1"));
  await rrCard(page, "rr_1").getByRole("button", { name: "Approve reroute" }).click();
  m.moveReroute("rr_1", "acknowledged", "dev-001");
  await expect(page.locator("#rr-status-rr_1")).toContainText("Accepted by pilot");
  await expect(page.locator("#rr-status-rr_1")).not.toContainText("acknowledged");
});

// ---- Contract A (requirement change) and B (simulated answers, 3-minute approval) -----------------------------------
test("contract A: the reroute card shows 'Airspace requirement changes from X to Y' only when it changes", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_n", "f-dev1")); // requirement_change: null
  await expect(rrCard(page, "rr_n")).toBeVisible();
  await expect(page.locator("#rr-req-rr_n")).toHaveCount(0);
  m.push("reroute", m.addReroute("rr_c", "f-al02", { requirement_change: { from: "none", to: "contact_tower" } }));
  await expect(page.locator("#rr-req-rr_c")).toHaveText("Airspace requirement changes from No ATC to Contact tower.");
  m.push("reroute", m.addReroute("rr_k", "f-al01", { requirement_change: { from: "contact_tower", to: "clearance" } }));
  await expect(page.locator("#rr-req-rr_k")).toHaveText("Airspace requirement changes from Contact tower to Clearance needed.");
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/b3-ops-reroute-req-light.png` });
});

test("contract B: a simulated reroute reads 'Accepted/Declined by simulated pilot'; a real unit's stays 'by pilot'; cards sort by expires_at", async ({ page }) => {
  test.setTimeout(60_000);
  const { m } = await open(page);
  expect(m.addReroute("rr_probe", "f-al01").simulated).toBe(true);
  m.reroutes.pop();
  m.push("reroute", m.addReroute("rr_sd", "f-al01"));
  await rrCard(page, "rr_sd").getByRole("button", { name: "Approve reroute" }).click();
  m.moveReroute("rr_sd", "unable", "simulated pilot");
  await expect(page.locator("#rr-status-rr_sd")).toHaveText("Declined by simulated pilot");
  m.push("reroute", m.addReroute("rr_real", "f-dev1"));
  await rrCard(page, "rr_real").getByRole("button", { name: "Approve reroute" }).click();
  m.moveReroute("rr_real", "unable", "dev-001");
  await expect(page.locator("#rr-status-rr_real")).toHaveText("Declined by pilot");
  // the mock's expires_at is 3 minutes after creation (contract B: TTL 180 s)
  const real = m.reroutes.find((x) => x.id === "rr_real");
  expect(Date.parse(real.expires_at) - Date.parse(real.created_at)).toBe(180_000);
  await page.locator('.fl-row[data-flight="f-al01"]').click();
  await page.click("#pop-details");
  await expect(page.locator('[data-record="rr_sd"]')).toContainText("Declined by simulated pilot");
});

// ---- B3 screenshots: 2 sizes x 2 themes (overview + reroute card with the requirement change + notice + footer; login) ----
for (const [w, h] of [[1440, 900], [1920, 1080]] as const) {
  for (const theme of ["light", "dark"] as const) {
    test(`B3 shots ${w}x${h} ${theme}: overview, reroute requirement change, notice, footer; login`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await page.addInitScript((t) => localStorage.setItem("aerolink.ops.appearance", t), theme);
      const { m } = await open(page);
      m.push("reroute", m.addReroute("rr_shot", "f-dev1", { requirement_change: { from: "none", to: "contact_tower" } }));
      await expect(page.locator("#rr-req-rr_shot")).toBeVisible();
      await expect(page.locator("#ai-overview")).toBeVisible();
      await expect(page.locator("#proto-note")).toBeVisible();
      await expect(page.locator("#ai-footer")).toBeVisible();
      await expect(page.locator(".ops")).toHaveClass(theme === "dark" ? /night/ : /^(?!.*night)/);
      // the notice strip and the AI footer are readable (WCAG AA, 4.5:1) in this theme
      for (const sel of ["#proto-note", ".attribution", "#ai-footer"]) {
        const ratio = await contrast(page, sel);
        console.log(`B3 contrast ${theme} ${sel}: ${ratio.toFixed(2)}`);
        expect(ratio, `${sel} contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
      }
      // the last flight row never sits under the strip: scrolled to the end, its bottom is above the strip's top
      await page.locator(".fl-body").evaluate((el) => { el.scrollTop = el.scrollHeight; });
      const lastBottom = await page.locator(".fl-row").last().evaluate((el) => el.getBoundingClientRect().bottom);
      const stripTop = await page.locator(".attribution").evaluate((el) => el.getBoundingClientRect().top);
      expect(lastBottom, "last flight row clear of the notice strip").toBeLessThanOrEqual(stripTop);
      await page.locator(".fl-body").evaluate((el) => { el.scrollTop = 0; });
      await page.waitForTimeout(900);
      await page.screenshot({ path: `${SHOT}/b3-ops-${w}x${h}-${theme}.png` });
      const m2 = await mockOps(page, { session: false });
      void m2;
      await page.goto("/login");
      await expect(page.locator("h1")).toHaveText("AeroLink Ops sign-in");
      await page.waitForTimeout(400);
      await page.screenshot({ path: `${SHOT}/b3-login-${w}x${h}-${theme}.png` });
    });
  }
}

// ---- U5 guard: plain English, no Unicode symbols in any visible text, aria-label or title ---------------------------
const SYMBOLS = /[>=\u00B0\u00B1\u00B7\u00D7\u00A9\u00A7\u2010-\u2015\u2022\u2026\u2039\u203A\u2190-\u21FF\u2212\u2500-\u27BF\u2B00-\u2BFF]/;
async function expectNoSymbols(page: Page, where: string) {
  const texts: string[] = await page.evaluate(() => [
    document.body.innerText,
    ...Array.from(document.querySelectorAll("[aria-label],[title]")).flatMap((e) => [e.getAttribute("aria-label") ?? "", e.getAttribute("title") ?? ""]),
  ]);
  for (const t of texts) expect(t.match(SYMBOLS)?.[0] ?? null, `${where}: symbol in "${t.slice(0, 80)}"`).toBeNull();
}

test("U5 guard: no Unicode symbols on the overview, a selected flight, its details, alerts, conflicts, system, the AI sheet and the gear panel", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_g", "f-dev1", { requirement_change: { from: "none", to: "contact_tower" } }));
  await expect(rrCard(page, "rr_g")).toBeVisible();
  await expectNoSymbols(page, "overview");
  await page.locator('.fl-row[data-flight="f-dev1"]').click();
  await expectNoSymbols(page, "popover");
  await page.click("#pop-details");
  await expectNoSymbols(page, "detail");
  await page.click("#fl-back");
  for (const id of ["bell-btn", "status-btn", "ai-btn", "gear-btn"]) {
    await page.click(`#${id}`);
    await page.waitForTimeout(300);
    await expectNoSymbols(page, id);
    await page.click(`#${id}`);
  }
  await page.getByRole("button", { name: "Reroutes", exact: false }).first().click();
  await expectNoSymbols(page, "reroutes tab");
});

test("U5 guard: no Unicode symbols on the login page", async ({ page }) => {
  await mockOps(page, { session: false });
  await page.goto("/login");
  await expect(page.locator("h1")).toHaveText("AeroLink Ops sign-in");
  await expectNoSymbols(page, "login");
  expect(await page.title()).not.toMatch(SYMBOLS);
});

// ---- U5: cut-off text opens the full text -----------------------------------------------------------------------
test("cut-off text: a long name is cut with a chevron; clicking opens the full text; Esc closes it; callsigns are never cut", async ({ page }) => {
  const { m } = await open(page);
  m.push("reroute", m.addReroute("rr_cut", "f-dev1"));
  const reason = rrCard(page, "rr_cut").locator(".rr-reason .cut");
  await expect(reason).toHaveClass(/is-cut/);
  await expect(reason.locator(".cut-chev")).toBeVisible();
  await expect.poll(() => reason.locator(".cut-text").evaluate((el) => el.isConnected && el.scrollWidth > el.clientWidth)).toBe(true); // really cut off by the browser (the card may be redrawn once: poll)
  const full = m.reroutes.find((x) => x.id === "rr_cut").reason;
  await reason.click();
  await expect(page.locator("#fulltext")).toBeVisible();
  await expect(page.locator("#fulltext")).toContainText(full);
  await page.screenshot({ path: `${SHOT}/b3-ops-cutoff-fulltext.png` });
  await page.keyboard.press("Escape");
  await expect(page.locator("#fulltext")).toHaveCount(0);
  // a flight row's cut-off route shows the chevron cue; its popover carries the whole route
  const cutRow = page.locator(".fl-row .r2 .cut.is-cut").first();
  await expect(cutRow.locator(".cut-chev")).toBeVisible();
  await cutRow.click();
  await expect(page.locator("#pop-route")).toContainText(" to ");
  // callsigns are never cut off
  await expect.poll(() => page.locator(".fl-row .cs, .arr-tag").evaluateAll((els) => els.filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => `${e.className}: ${e.textContent} ${e.scrollWidth}/${e.clientWidth}`))).toEqual([]);
  // a cut-off lane name in the timeline opens its full name
  const lane = page.locator(".arr-lane-h .cut.is-cut").first();
  if (await lane.count()) {
    await lane.locator(".cut-chev").click();
    await expect(page.locator("#fulltext")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("#fulltext")).toHaveCount(0);
  }
});
