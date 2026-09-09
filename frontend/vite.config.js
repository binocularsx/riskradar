import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard talks to the API on a different port in development. Proxying
// rather than enabling wide CORS keeps the session cookie same-origin, which is
// what SameSite=Lax needs to work the way D12 intends.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
