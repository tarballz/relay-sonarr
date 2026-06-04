import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build straight into the backend's static dir so FastAPI serves one origin.
// In dev, proxy /api to the backend so the SPA and API share an origin too.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
