import { defineConfig } from "@playwright/test";

const baseURL =
  process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:5173";
const target = new URL(baseURL);
const port = target.port || (target.protocol === "https:" ? "443" : "80");

export default defineConfig({
  testDir: "./e2e",
  timeout: 240_000,
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  webServer: {
    command: `npm run dev -- --host ${target.hostname} --port ${port}`,
    url: baseURL,
    reuseExistingServer: true,
  },
});
