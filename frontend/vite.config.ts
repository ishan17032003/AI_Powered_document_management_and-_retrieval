import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA talks to the FastAPI backend on :8000. We proxy /api during dev so
// the browser sees a same-origin API and CORS is a non-issue.
//
// VITE_API_TARGET lets docker-compose.dev.yml override the proxy target to the
// internal service name (http://backend:8000) without touching this file.
const apiTarget = process.env.VITE_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,          // bind to 0.0.0.0 so the port is reachable from Docker host
    port: 5173,
    strictPort: true,
    watch: {
      usePolling: true,  // reliable file-change detection inside Docker bind mounts
      interval: 300,
    },
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
});
