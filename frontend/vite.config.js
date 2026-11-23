import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.js",
    css: false,
    // The threads pool fails to start its workers on Windows (the pool times
    // out before the worker answers), so the suite runs on forks instead.
    pool: "forks",
    // A component test that renders a PDF blob and a jsdom tree is slow on a
    // loaded machine, so 5s is not a realistic budget for the whole pool.
    testTimeout: 30000,
  },
});
