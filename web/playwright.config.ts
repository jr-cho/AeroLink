import { defineConfig, devices } from "@playwright/test";

// Tests run against `vite preview` (production build) with every /api call mocked in the test.
// Parallel agents: each one sets its own PW_PORT (pilot 4173 default, Ops/web map 4174, reviewers 4181+). Each port
// gets its own build folder, artifacts folder and lock (/tmp/aerolink-pw-<port>.lock), so runs never share a server.
const PORT = process.env.PW_PORT ?? "4173";
const OUT = PORT === "4173" ? "dist" : `dist-${PORT}`;
const BASE = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "tests",
  outputDir: PORT === "4173" ? "test-results/artifacts" : `test-results/artifacts-${PORT}`,
  timeout: 30_000,
  reporter: [["list"]],
  use: { baseURL: BASE, viewport: { width: 1280, height: 800 }, launchOptions: { args: ["--use-gl=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] } },
  // WebKit isn't installed here: the pilot project emulates an iPhone 15 (viewport, UA, touch) in Chromium.
  projects: [
    { name: "webmap", testMatch: /webmap\.spec\.ts/ },
    { name: "ops", testMatch: /ops\.spec\.ts/ },
    { name: "pilot", timeout: 60_000, testMatch: /pilot.*\.spec\.ts/, use: { ...devices["iPhone 15"], browserName: "chromium", defaultBrowserType: "chromium" } },
    // E6.12: the pilot screen is landscape-first (phone or tablet mounted in the cockpit).
    { name: "pilot-phone-landscape", timeout: 60_000, testMatch: /pilot\.spec\.ts/, use: { ...devices["iPhone 15"], browserName: "chromium", defaultBrowserType: "chromium", viewport: { width: 932, height: 430 } } },
    { name: "pilot-ipad-landscape", timeout: 60_000, testMatch: /pilot\.spec\.ts/, use: { ...devices["iPad (gen 7) landscape"], browserName: "chromium", defaultBrowserType: "chromium", viewport: { width: 1180, height: 820 } } },
  ],
  // Build first: `vite preview` serves dist/, so a stale bundle would test old code. `vite build` (not `npm run build`)
  // skips the repo-wide tsc, which must not block UI tests; typecheck is a separate step.
  webServer: { command: `npx vite build --outDir ${OUT} --emptyOutDir && npx vite preview --outDir ${OUT} --port ${PORT} --strictPort`, url: BASE, reuseExistingServer: false, timeout: 120_000 },
});
