import { expect, test, type Page } from "@playwright/test";
import { mockApi, type MockState } from "./mock.ts";

/** The host the app is served from (the port is per agent, set by PW_PORT in playwright.config.ts). */
const appHost = (): string => new URL(test.info().project.use.baseURL ?? "http://localhost:4173").host;
const SHOT = "test-results/screenshots";

async function open(page: Page, opts = {}): Promise<{ m: MockState; errors: string[]; hosts: Set<string> }> {
  const errors: string[] = [];
  const hosts = new Set<string>();
  page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("request", (r) => { const u = new URL(r.url()); if (u.protocol.startsWith("http")) hosts.add(u.host); });
  const m = await mockApi(page, opts);
  await page.goto("/");
  await expect(page.locator("#map canvas")).toBeVisible();
  await page.waitForFunction(() => !!(window as any).__map?.getSource("aircraft") && !!document.querySelector("#dir-go"));
  return { m, errors, hosts };
}

const mapFeatures = (page: Page, layer: string) => page.evaluate((l) => (window as any).__map.queryRenderedFeatures({ layers: [l] }).length, layer);

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

async function directions(page: Page, from: string, to: string) {
  await page.selectOption("#dir-from", from);
  await page.selectOption("#dir-to", to);
  await expect(page.locator(".route-card").first()).toBeVisible();
}

test("map shows corridors, sites, no-go volumes, no console errors, same-origin only", async ({ page }) => {
  const { errors, hosts } = await open(page);
  const counts = await page.evaluate(() => {
    const m = (window as any).__map;
    return { corridors: m.getSource("corridors").serialize().data.features.length, sites: m.getSource("sites").serialize().data.features.length, nogo: m.getSource("nogo").serialize().data.features.length, extrusion: m.getLayer("nogo-extrusion").type };
  });
  expect(counts.corridors).toBe(45);
  expect(counts.sites).toBe(43);
  expect(counts.nogo).toBeGreaterThan(10);
  expect(counts.extrusion).toBe("fill-extrusion");
  const flagged = await page.evaluate(() => (window as any).__map.getSource("corridors").serialize().data.features.filter((f: any) => f.properties.coordination || f.properties.clearance).length);
  expect(flagged).toBeGreaterThan(0);
  await expect(page.locator(".attribution")).toContainText("adsb.lol");
  await expect(page.locator(".attribution")).toContainText("Map data from OpenStreetMap and Protomaps");
  await expect(page.locator(".attribution")).toContainText("FAA ADDS");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${SHOT}/01-map.png` });
  await page.click("#tilt");
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/02-map-3d.png` });
  expect([...hosts]).toEqual([appHost()]);
  expect(errors.filter((e) => !/basemap/.test(e) && !/404/.test(e))).toEqual([]);
});

test("traffic layer colours corridors and the slider changes the forecast", async ({ page }) => {
  await open(page);
  await expect(page.locator("#traffic-slider")).toBeVisible();
  const levels = () => page.evaluate(() => { const c: Record<string, number> = {}; for (const f of (window as any).__map.getSource("corridors").serialize().data.features) c[f.properties.level] = (c[f.properties.level] ?? 0) + 1; return c; });
  await expect.poll(async () => (await levels()).low).toBeGreaterThan(0);
  const now = await levels();
  await page.locator("#traffic-slider").fill("30");
  await expect(page.locator("#traffic-slider-label")).toHaveText("in 30 min");
  const later = await levels();
  expect(later.heavy ?? 0).toBeGreaterThan(now.heavy ?? 0);
  await page.locator("#traffic-slider").fill("45");
  await expect(page.locator("#traffic-slider-label")).toHaveText("in 45 min");
  await expect(page.locator("#traffic-note")).toHaveText(""); // the server serves in_45_min, so no "horizon ends sooner" note
  const at45 = await levels();
  expect(at45.heavy ?? 0).toBeGreaterThan(0);
  await page.screenshot({ path: `${SHOT}/03-traffic-30min.png` });
  await page.locator("#tab-layers").click();
  await page.locator("#layer-traffic").uncheck();
  await expect(page.locator("#traffic-slider")).toBeHidden();
});

test("live aircraft: ten drawn, stale / lost / off-route distinguished, alerts listed", async ({ page }) => {
  await open(page);
  await expect.poll(() => page.evaluate(() => (window as any).__map.getSource("aircraft").serialize().data.features.length)).toBe(10);
  await expect.poll(() => page.evaluate(() => { const k: Record<string, number> = {}; for (const f of (window as any).__map.getSource("aircraft").serialize().data.features) k[f.properties.kind] = (k[f.properties.kind] ?? 0) + 1; return k.lost ?? 0; })).toBe(1);
  // stale comes from the server's `stale` flag at once (no waiting for the client to notice)
  await expect.poll(() => page.evaluate(() => (window as any).__map.getSource("aircraft").serialize().data.features.filter((f: any) => f.properties.kind === "stale").length)).toBe(1);
  const colours = await page.evaluate(() => new Set((window as any).__map.getSource("aircraft").serialize().data.features.map((f: any) => f.properties.color)).size);
  expect(colours).toBe(2);
  const off = await page.evaluate(() => (window as any).__map.getSource("aircraft").serialize().data.features.filter((f: any) => f.properties.off_route).length);
  expect(off).toBe(1);
  await expect(page.locator("#alert-chip")).toContainText("alert");
  await page.locator("#alert-chip").click();
  await expect(page.locator(".alert-item.off-route")).toBeVisible();
  await expect(page.locator(".alert-item.lost")).toBeVisible();
  await expect(page.locator(".alert-item.out-of-region")).toBeVisible();
  await page.screenshot({ path: `${SHOT}/04-aircraft-alerts.png` });
});

test("directions: vehicle type limits destinations, options show time/ETA/reason/summary/fastest and response time", async ({ page }) => {
  const { m } = await open(page, { planDelay: 150 });
  const optionsFor = (sel: string) => page.locator(`${sel} option`).allTextContents();
  const pax = await optionsFor("#dir-to");
  expect(pax.some((t) => t.includes("SunTrax"))).toBe(true);
  expect(pax.some((t) => t.includes("Tampa General"))).toBe(false);
  await page.click('[data-vt="medical"]');
  const med = await optionsFor("#dir-to");
  expect(med.some((t) => t.includes("Tampa General"))).toBe(true);
  expect(med.some((t) => t.includes("SunTrax"))).toBe(false);
  await page.click('[data-vt="passenger"]');
  await directions(page, "ev-water-st", "ev-orl");
  const cards = page.locator(".route-card");
  expect(await cards.count()).toBeGreaterThanOrEqual(1);
  await expect(cards.first()).toContainText("Recommended");
  // the mock's rank 1 is also the lowest predicted time (est_time_s + expected_wait_s), so it carries both badges
  await expect(cards.first().locator(".chip.fastest")).toHaveText("Fastest");
  await expect(page.locator(".route-card .chip.fastest")).toHaveCount(1);
  await expect(cards.first()).toContainText("Arrive");
  await expect(page.locator("#dir-summary")).toContainText("routing");
  // L-FIX (C-07): the AI route summary is labelled as AI, with the verify line; the map carries the prototype notice
  await expect(page.locator("#dir-summary .ai-mark")).toHaveText("AI");
  await expect(page.locator("#dir-summary-note")).toHaveText("AI-generated; verify before acting.");
  await expect(page.locator("#proto-note")).toHaveText("Prototype. Not certified. Not for flight. Not ATC.");
  await expect(page.locator(".attribution")).toContainText("Map data from OpenStreetMap and Protomaps");
  await expect(page.locator('.attribution a:text-is("OpenStreetMap")')).toHaveAttribute("href", "https://www.openstreetmap.org/copyright");
  const timing = await page.locator("#dir-timing").textContent();
  expect(timing).toMatch(/\d+ ms/);
  expect(m.planCalls[0].body).toMatchObject({ vehicle_type: "passenger", origin_node: "ev-water-st", destination_node: "ev-orl" });
  const routes = await page.evaluate(() => (window as any).__map.getSource("routes").serialize().data.features);
  expect(routes.length).toBeGreaterThanOrEqual(1);
  expect(new Set(routes.map((r: any) => r.properties.color)).size).toBe(routes.length);
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/05-directions.png` });
});

test("method toggle re-plans open directions and shows both methods side by side", async ({ page }) => {
  const { m } = await open(page);
  await directions(page, "ev-water-st", "ev-orl");
  const firstId = await page.locator(".route-card").first().locator(".rc-reason").textContent();
  const before = m.planCalls.length;
  await page.click('[data-method="classical"]');
  await expect(page.locator(".chip.method")).toHaveText("Classical");
  expect(m.planCalls.length).toBe(before + 1);
  expect(m.planCalls[m.planCalls.length - 1].method).toBe("classical");
  const classicalReason = await page.locator(".route-card").first().locator(".rc-reason").textContent();
  expect(classicalReason).not.toBe(firstId);
  await expect(page.locator("#dir-compare")).toBeVisible();
  await expect(page.locator("#dir-compare")).toContainText("AeroLink");
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${SHOT}/06-method-classical.png` });
  await page.click('[data-method="aerolink"]');
  await expect(page.locator(".chip.method")).toHaveText("AeroLink");
});

test("forecaster toggle shows loading and reports failures", async ({ page }) => {
  const { m } = await open(page);
  await page.selectOption("#forecaster", "chronos");
  await expect(page.locator("#forecaster-note")).toContainText("Loading");
  await expect(page.locator("#forecaster")).toBeDisabled();
  await expect(page.locator("#forecaster")).toBeEnabled();
  expect(m.forecaster).toBe("chronos");
  await expect(page.locator("#forecaster")).toHaveValue("chronos");
});

test("forecaster failure is shown and the old value is restored", async ({ page }) => {
  await open(page, { failForecaster: true });
  await page.selectOption("#forecaster", "aerolink-gbm");
  await expect(page.locator("#forecaster-note")).toContainText("forecaster unavailable");
  await expect(page.locator("#forecaster")).toHaveValue("cv");
});

test("planned flights show the ETA window, server cones and the medical view uses prediction history", async ({ page }) => {
  await open(page);
  await page.click("#tab-flights");
  await expect(page.locator(".row-card.flight")).toHaveCount(2);
  await expect(page.locator(".row-card.flight").first()).toContainText("window");
  await page.click("#tab-layers");
  await page.locator("#layer-planned").check();
  await page.locator("#layer-cones").check();
  const vis = await page.evaluate(() => { const m = (window as any).__map; return [m.getLayoutProperty("planned", "visibility"), m.getLayoutProperty("cones", "visibility")]; });
  expect(vis).toEqual(["visible", "visible"]);
  expect(await page.evaluate(() => (window as any).__map.getSource("planned").serialize().data.features.length)).toBe(2);
  // the server's cones (one ribbon per planned flight), not the ten client-side wedges
  await expect.poll(() => page.evaluate(() => (window as any).__map.getSource("cones").serialize().data.features.map((f: any) => f.properties.flight_id).sort())).toEqual(["f1", "f2"]);
  await expect(page.locator("#cones-note")).toContainText("server's forecast");
  await page.screenshot({ path: `${SHOT}/07-planned-cones.png` });
  await page.click("#tab-medical");
  await expect(page.locator(".medical-flight")).toHaveCount(1);
  await expect(page.locator(".medical-flight")).toContainText("predicted arrival changed");
  await expect(page.locator(".medical-flight .affected")).toHaveCount(1);
  await expect(page.locator(".medical-flight .affected")).toContainText("AL-01");
  await expect(page.locator(".medical-flight .affected")).toContainText("medical flight AL-06 planned on shared nodes");
  await page.screenshot({ path: `${SHOT}/08-medical.png` });
});

test("without server cones the client estimate is used and labelled as an estimate", async ({ page }) => {
  await open(page, { noCones: true });
  await page.click("#tab-layers");
  await expect(page.locator("#cones-note")).toContainText("estimate");
  await expect.poll(() => page.evaluate(() => (window as any).__map.getSource("cones").serialize().data.features.length)).toBeGreaterThan(5);
});

test("AI impact panel: B1 vs A5 from the ablation table, equal shown as equal; scenario control lists the server's scenarios", async ({ page }) => {
  const { m } = await open(page);
  await page.click("#tab-impact");
  const row = (k: string) => page.locator(`#impact-panel tr[data-metric="${k}"]`);
  await expect(row("door_to_door_mean_s")).toBeVisible();
  await expect(row("door_to_door_mean_s").locator("td.delta")).toHaveText("equal"); // 1010 vs 1010: no spin
  await expect(row("eta_error_mean_s").locator("td.delta")).toContainText("better");
  await expect(row("eta_coverage")).toContainText("80%");
  await expect(row("suggestions_accepted")).toBeVisible();
  await expect(row("ground_delay_mean_s")).toBeVisible();
  await expect(row("eta_error_cruise_mean_s")).toBeVisible();
  await expect(page.locator("#impact-panel .per-scenario tbody tr")).toHaveCount(2);
  await expect(page.locator("#impact-panel")).toContainText("flights planned");
  await page.screenshot({ path: `${SHOT}/09-impact.png` });
  await page.click("#tab-scenario");
  await expect(page.locator('[data-scenario="fdot_high_peak"]')).toBeVisible(); // from /api/scenarios, not hard-coded
  await expect(page.locator('[data-scenario="nominal_day"] .chip')).toHaveText("current");
  await page.click('[data-scenario="i4_peak"]');
  await expect(page.locator("#scenario-panel")).toContainText("i4_peak requested");
  expect(m.scenario).toBe("i4_peak");
  await page.screenshot({ path: `${SHOT}/10-scenario.png` });
});

test("AI impact panel falls back to the M4 runs when the server has no ablation table", async ({ page }) => {
  await open(page, { noAblations: true });
  await page.click("#tab-impact");
  await expect(page.locator("table.metrics tr[data-config=B1]").first()).toBeVisible();
  await expect(page.locator("#impact-panel")).toContainText("M5 ablation table is not available");
});

test("tablet width layout and keyboard access", async ({ page }) => {
  await page.setViewportSize({ width: 820, height: 1100 });
  await open(page);
  await expect(page.locator(".directions")).toBeVisible();
  await page.keyboard.press("Tab");
  const focused = await page.evaluate(() => document.activeElement?.tagName);
  expect(["BUTTON", "SELECT", "INPUT", "A", "CANVAS"]).toContain(focused);
  await page.locator("#tab-layers").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#layer-nogo")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
  await page.screenshot({ path: `${SHOT}/11-tablet.png` });
});

test("device route mounts the pilot module", async ({ page }) => {
  await mockApi(page); // unmocked /api/devices answers 404: the pilot page shows its "Unknown unit" screen
  await page.goto("/device/unit-7");
  await expect(page.locator("main.pair-screen")).toContainText("unit-7");
});

const aircraftFeatures = (page: Page) => page.evaluate(() => (window as any).__map.getSource("aircraft").serialize().data.features.map((f: any) => f.properties));

test("signed in: method, forecaster and scenario work and no sign-in line is shown", async ({ page }) => {
  const { m } = await open(page);
  await expect(page.locator(".signin-note")).toBeHidden();
  await expect(page.locator('[data-method="classical"]')).toBeEnabled();
  await expect(page.locator("#forecaster")).toBeEnabled();
  await page.click('[data-method="classical"]');
  await expect.poll(() => m.method).toBe("classical");
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/webmap-auth-signed-in.png` });
});

test("signed out: operator controls are disabled behind one sign-in line, directions still work", async ({ page }) => {
  const { m } = await open(page, { signedOut: true });
  const note = page.locator(".controls .signin-note");
  await expect(note).toBeVisible();
  await expect(note).toHaveText("Operator sign-in required");
  await expect(note).toHaveAttribute("href", "/login?next=/");
  await expect(page.locator('[data-method="classical"]')).toBeDisabled();
  await expect(page.locator('[data-method="aerolink"]')).toBeDisabled();
  await expect(page.locator("#forecaster")).toBeDisabled();
  await page.click("#tab-scenario");
  await expect(page.locator('[data-scenario="i4_peak"]')).toBeDisabled();
  await expect(page.locator("#scenario-panel .signin-note")).toBeVisible();
  await directions(page, "ev-water-st", "ev-orl"); // POST /api/plan stays open to everyone
  expect(m.planCalls.length).toBeGreaterThan(0);
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT}/webmap-auth-signed-out.png` });
});

test("a 401 from a PUT switches the controls to the signed-out state", async ({ page }) => {
  const { m } = await open(page); // /api/auth/me says signed in, but the session has since ended
  await page.route("**/api/method", (r) => (r.request().method() === "PUT" ? r.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"Operator sign-in required"}' }) : r.fallback()));
  await page.route("**/api/scenario", (r) => (r.request().method() === "PUT" ? r.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"Operator sign-in required"}' }) : r.fallback()));
  await page.click('[data-method="classical"]');
  await expect(page.locator(".controls .signin-note")).toBeVisible();
  await expect(page.locator('[data-method="aerolink"]')).toBeDisabled();
  await expect(page.locator("#forecaster")).toBeDisabled();
  await expect(page.locator("#method-toggle ~ .tb-note")).toHaveText(""); // no "Failed" text
  expect(m.method).toBe("aerolink");

  // scenario PUT 401 as well (fresh page, so the method state above does not interfere)
  await page.reload();
  await page.waitForFunction(() => !!document.querySelector("#dir-go"));
  await page.click("#tab-scenario");
  await page.click('[data-scenario="i4_peak"]');
  await expect(page.locator("#scenario-panel .signin-note")).toBeVisible();
  await expect(page.locator('[data-scenario="nominal_day"]')).toBeDisabled();
});

test("on_ground aircraft: small grey dot, no heading arrow, not an alert, callsign as label", async ({ page }) => {
  await open(page, { withGround: true });
  await expect.poll(async () => (await aircraftFeatures(page)).length).toBe(11);
  const feats = await aircraftFeatures(page);
  const ground = feats.filter((f: any) => f.kind === "ground");
  expect(ground.map((f: any) => f.id)).toEqual(["AL-11"]);
  expect(ground[0].label).toBe("N11PAD");
  expect(feats.find((f: any) => f.id === "AL-01").label).toBe("CS-1");
  // no heading arrow for it, one for each of the ten flying aircraft
  await expect.poll(() => page.evaluate(() => (window as any).__map.getSource("aircraft-heading").serialize().data.features.length)).toBe(10);
  // styled as a small muted dot
  const paint = await page.evaluate(() => { const m = (window as any).__map; return { r: m.getLayer("aircraft").paint.get("circle-radius"), c: m.getLayer("aircraft").paint.get("circle-color") }; });
  expect(JSON.stringify(paint)).toContain("3.5");
  // alerts unchanged by it (and named by callsign)
  await page.locator("#alert-chip").click();
  await expect(page.locator(".alert-item")).toHaveCount(3);
  await expect(page.locator(".alert-item.lost")).toContainText("CS-9");
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${SHOT}/webmap-ground.png` });
});

test.describe("live ADS-B layer", () => {
  const adsbCount = (page: Page) => page.evaluate(() => (window as any).__map.getSource("adsb-traffic").serialize().data.features.length);

  test("quiet markers from the feed, toggle hides them, attribution appears once", async ({ page }) => {
    const { m } = await open(page);
    await expect.poll(() => adsbCount(page)).toBe(2);
    const labels = await page.evaluate(() => (window as any).__map.getSource("adsb-traffic").serialize().data.features.map((f: any) => f.properties.label));
    expect(labels).toEqual(["N123AB", "abcdef"]);
    await expect(page.locator(".attribution")).toContainText("Aircraft data: adsb.lol (ODbL)"); // one fixed credit, the same wording as Ops (was: the feed's long text)
    expect(((await page.locator(".attribution").textContent()) ?? "").match(/ODbL/g)).toHaveLength(1);
    // the mock's own aircraft raise 3 alerts; ADS-B adds none
    expect(await page.locator("#alert-chip").textContent()).toBe("3 alerts");
    expect(m.adsbCalls).toBe(1);
    await page.click("#tab-layers");
    await expect(page.locator("#adsb-note")).toBeHidden();
    await page.evaluate(() => (window as any).__map.jumpTo({ center: [-81.5, 28.4], zoom: 10.5 }));
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${SHOT}/webmap-adsb.png` });
    expect(((await page.locator(".attribution").textContent()) ?? "").match(/Aircraft data/g)).toHaveLength(1);
    await page.uncheck("#layer-adsb");
    expect(await page.evaluate(() => (window as any).__map.getLayoutProperty("adsb-icons", "visibility"))).toBe("none");
    expect(((await page.locator(".attribution").textContent()) ?? "").match(/ODbL/g)).toHaveLength(1);
    await expect(page.locator(".attribution")).toContainText("adsb.lol");
    await page.check("#layer-adsb");
    expect(await page.evaluate(() => (window as any).__map.getLayoutProperty("adsb-icons", "visibility"))).toBe("visible");
  });

  test("feed down: nothing drawn, one muted note in the layers panel", async ({ page }) => {
    await open(page, { adsb: "down" });
    await page.click("#tab-layers");
    await expect(page.locator("#adsb-note")).toHaveText("ADS-B unavailable");
    expect(await adsbCount(page)).toBe(0);
    expect(((await page.locator(".attribution").textContent()) ?? "").match(/ODbL/g)).toHaveLength(1);
  });
});

for (const [w, h] of [[1440, 900], [1920, 1080]] as const) {
  test(`B3 shot ${w}x${h}: the AI route summary with the AI label, the verify line and the notice`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h });
    await open(page);
    await directions(page, "ev-water-st", "ev-orl");
    await expect(page.locator("#dir-summary .ai-mark")).toHaveText("AI");
    await expect(page.locator("#proto-note")).toBeVisible();
    for (const sel of ["#proto-note", ".attribution", "#dir-summary-note"]) {
      const ratio = await contrast(page, sel);
      console.log(`B3 contrast webmap ${sel}: ${ratio.toFixed(2)}`);
      expect(ratio, `${sel} contrast`).toBeGreaterThanOrEqual(4.5);
    }
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT}/b3-webmap-${w}x${h}.png` });
  });
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

test("U5 guard: no Unicode symbols on the map, the directions with a summary, and every tab", async ({ page }) => {
  await open(page);
  await expectNoSymbols(page, "map");
  await directions(page, "ev-water-st", "ev-orl");
  await expectNoSymbols(page, "directions");
  for (const name of ["Layers", "Flights", "Medical", "AI impact", "Scenario"]) {
    await page.locator(".tab", { hasText: new RegExp(`^${name}$`) }).evaluate((el) => (el as HTMLElement).click()); // a plain click can be covered by the alerts list
    await page.waitForTimeout(300);
    await expectNoSymbols(page, name);
  }
});
