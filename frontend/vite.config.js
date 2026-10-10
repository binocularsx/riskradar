import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard talks to the API on a different port in development. Proxying
// rather than enabling wide CORS keeps the session cookie same-origin, which is
// what SameSite=Lax needs to work the way D12 intends.
const api = { target: 'http://127.0.0.1:8000', changeOrigin: true }

// Sharing the console from this PC (scripts/share.py): the built console is
// served by `vite preview` behind a Cloudflare Tunnel. The public address is for
// people in a browser only. Machine callers (payment intake, events, the support
// team's routes, the bank feed) all authenticate with X-API-Key, and a browser
// using the console never sends one, so any request carrying a key is refused
// here: the development keys are public in this repository, and the simulator
// keeps reaching the API directly on 127.0.0.1:8000.
const browserOnly = {
  ...api,
  bypass(req) {
    if (req.headers['x-api-key']) return false // Vite answers 404
  },
}

// Extra hostnames for a named tunnel on your own domain, comma-separated.
const shareHosts = (process.env.RISKRADAR_SHARE_HOSTS || '').split(',').map((h) => h.trim()).filter(Boolean)

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/v1': api, '/health': api },
  },
  preview: {
    host: '127.0.0.1', // only cloudflared, on this PC, connects to it
    port: 4173,
    strictPort: true,
    allowedHosts: ['.trycloudflare.com', ...shareHosts],
    proxy: { '/v1': browserOnly, '/health': api },
  },
})
