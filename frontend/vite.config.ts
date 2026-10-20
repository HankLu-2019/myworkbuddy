/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: process.env.MWB_API_URL ?? "http://localhost:8000", changeOrigin: false },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    coverage: { include: ["src/**/*.{ts,tsx}"], exclude: ["src/main.tsx", "src/**/*.test.*", "src/test-setup.ts"] },
  },
});
