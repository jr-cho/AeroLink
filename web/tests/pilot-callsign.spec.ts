import { expect, test } from "@playwright/test";
import { CODE, DEVICE, mockPilot } from "./pilot-mock.ts";

/** G2: the pilot screen shows the unit's callsign once, quietly, in the status details. Nothing to type or edit. */
test("status details show the unit's callsign, read-only; nothing on the home screen", async ({ page }) => {
  const m = await mockPilot(page);
  await page.route(/\/api\/devices\/dev-001$/, (route) => {
    if (route.request().method() !== "GET" || !m.paired) return route.fallback();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...m.stateBody(), callsign: "N123AL" }) });
  });
  await page.goto(`/device/${DEVICE}?code=${CODE}`);
  await expect(page.locator("#type-badge")).toBeVisible();
  await expect(page.locator("#link")).toHaveText("Live");
  await expect(page.locator("body")).not.toContainText("N123AL"); // not on the home screen: nothing extra to read
  await page.click("#status-btn");
  await expect(page.locator("#st-callsign")).toHaveText("N123AL");
  await expect(page.locator("#status-panel input")).toHaveCount(0); // never editable
});

test("a unit registered before callsigns existed shows no callsign row", async ({ page }) => {
  await mockPilot(page);
  await page.goto(`/device/${DEVICE}?code=${CODE}`);
  await expect(page.locator("#type-badge")).toBeVisible();
  await page.click("#status-btn");
  await expect(page.locator("#st-online")).toBeVisible();
  await expect(page.locator("#st-callsign")).toHaveCount(0);
});
