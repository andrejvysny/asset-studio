import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server proxies API + ComfyUI to the running library service (same origin as production).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8190",
      "/comfy": { target: "http://127.0.0.1:8190", ws: true },
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 2000 },
});
