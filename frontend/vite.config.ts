import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite proxies same-origin paths to the FastAPI backend so the dev
// browser never has to deal with CORS. Override via the VITE_BACKEND
// env var on the shell when starting the dev server.
export default defineConfig(({ mode: _mode }) => {
  const backend = (globalThis as { process?: { env: Record<string, string | undefined> } })
    .process?.env?.VITE_BACKEND ?? "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": { target: backend, changeOrigin: true },
        "/health": { target: backend, changeOrigin: true },
        "/stats": { target: backend, changeOrigin: true },
      },
    },
  };
});
