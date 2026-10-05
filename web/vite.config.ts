import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The app lives under /ui/, as it always has, so links people kept still work.
// In development the api runs beside it on :8000 and /api is passed through.
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  server: {
    host: true,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: { target: "es2023", sourcemap: true },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"] },
});
