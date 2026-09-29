import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server proxies the Studio API (same origin as production). The browser never talks to ComfyUI.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8190",
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 2000 },
});
