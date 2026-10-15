import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The API is served from the same origin in production (FastAPI mounts dist/).
// In development Vite proxies /api to the backend, so no CORS handling is needed.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // Bind explicitly: Vite's default ("localhost") resolves to ::1 only, so
    // http://127.0.0.1:5173 would be refused and any tooling that uses the IPv4
    // form would fail. Pinning 127.0.0.1 keeps the printed URL, the proxy target
    // and the browser all on the same address family.
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8770",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // ECharts is large; splitting it keeps the app shell fast to load.
        manualChunks: {
          echarts: ["echarts"],
          vendor: ["react", "react-dom", "react-router-dom"],
        },
      },
    },
  },
});
