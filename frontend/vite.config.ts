import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/v1": {
        target: process.env.VITE_GATEWAY_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
        login: resolve(__dirname, "login.html"),
        account: resolve(__dirname, "account.html"),
        wishlists: resolve(__dirname, "wishlists.html"),
        report: resolve(__dirname, "report.html"),
        school: resolve(__dirname, "school.html"),
      },
    },
  },
});
