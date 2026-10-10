# Risk Radar console (frontend)

React + Vite single-page app. It calls the API on **relative paths**
(`/v1/...`, `/health`) and the session is a first-party `SameSite=Lax` cookie
(D12, D95, D101b), so the browser must see one origin.

## Local development

    npm ci
    npm run dev      # http://localhost:5173, proxies /v1 and /health to 127.0.0.1:8000

## Vercel

The Vercel project uses Root Directory `frontend`, the Vite preset, and tracks
the `integration` branch for production.

`vercel.json` rewrites `/v1/*` and `/health` to the API host and falls back to
`index.html` for client-side routes. **Replace both `REPLACE_WITH_API_HOST`
entries with the API's public HTTPS host before the console can log in.**

Until the API is hosted, the page loads but login fails. This is expected.

To verify after the API is up: sign in, then confirm a live alert arrives
(the console uses `EventSource('/v1/stream')`, which must survive the rewrite).
If the stream does not survive Vercel's proxy, serve the console and API from
one origin behind Caddy instead (`deploy/README.md`, D101b).

The cloud instance must ingest only the simulator's synthetic traffic (D101a).
