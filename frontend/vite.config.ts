import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the UI runs on :5173 and proxies /api to the FastAPI server on :8000.
// `vite preview` serves the production build (what scripts/share.sh shares through a Cloudflare
// quick tunnel) on :4173 with the same proxy. A tunnel sends its own Host name, so those
// *.trycloudflare.com names are allowed; localhost and 127.0.0.1 are always allowed.
const api = { "/api": "http://127.0.0.1:8000" };
const tunnelHosts = [".trycloudflare.com"];

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: api, allowedHosts: tunnelHosts },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy: api, allowedHosts: tunnelHosts },
});
