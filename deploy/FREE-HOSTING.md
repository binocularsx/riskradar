# A free, always-on home for the live demo

The live console currently depends on Chidera's laptop: `scripts/share.py` opens a
Cloudflare quick tunnel to the API running on it, and rewrites `frontend/vercel.json`
so the Vercel site forwards to that tunnel. It works, but it has three faults that
matter before a defence:

* the site is up only while that laptop is **on, awake and online**;
* a quick tunnel gets a **new address every start**, so `vercel.json` must be
  re-pointed and redeployed each time;
* if that pointer is ever lost — it reverted to the `REPLACE_WITH_API_HOST`
  placeholder during the PR #3 merge — the live site fails at the password step
  with a message blaming the user's credentials.

None of that is what the decision log asks for. **D101b already specifies the
answer**: one origin, a TLS-terminating reverse proxy (Caddy, auto-HTTPS) serving
the built console and proxying `/v1`, `/v1/stream` and `/health` to the API. That
is written and sitting in this directory. The tunnel was the improvisation; this
document is the way back to the documented design.

## What already exists

| File | What it is |
|---|---|
| `deploy/Dockerfile` | the backend image (API, workers, clock sweep) |
| `deploy/Dockerfile.web` | Caddy plus the built single-page console |
| `deploy/docker-compose.yml` | base stack: PostgreSQL 16, API, workers, clocks |
| `deploy/docker-compose.prod.yml` | the public edge: Caddy on 80/443, certificates persisted |
| `deploy/Caddyfile` | one origin, automatic HTTPS, SSE flushed straight through |
| `deploy/README.md` | the production runbook and the D101c checklist |
| `.env.example` | every setting the stack reads |

So the deployment is written. The only thing missing is a machine to run
`docker compose up` on.

> **Read this before you start.** D87 records the compose as *"not exercised here
> (D30)"* — it has never actually been run. This deploy will be the first time.
> Budget time for it to fail the first few attempts, and do it **well before** the
> defence, not the week of it.

## Where to run it, free

Checked October 2026. The free-hosting landscape has thinned badly:

| Option | Always on? | Free? | Verdict |
|---|---|---|---|
| **Oracle Cloud Always Free** (ARM Ampere A1) | yes | yes, no expiry | **recommended** — runs this compose unchanged |
| Google Cloud Always Free (`e2-micro`) | yes | yes | 1 vCPU / 1 GB is too tight for Postgres + API + workers |
| Render | no — sleeps after 15 min | free Postgres **expires after 30 days** | the expiry lands inside the 5-week window |
| Fly.io | n/a | no free tier for new accounts | out |
| Koyeb | sleeps | no free plan for new accounts | out |
| Railway | n/a | no free tier | out |

Oracle's Always Free ARM allowance was halved in 2026 to **2 OCPU / 12 GB**, which
is still comfortably more than this stack needs. It does not expire.

**The risk, stated plainly:** Oracle signup rejections are widely reported, approval
depends on country, and `A1.Flex` capacity errors are common in some regions. Try
this first, because if it works nothing else is needed — but find out early.

## Steps

Everything below is yours to run. Per D101c, credentials never pass through Claude:
Claude writes the config and the steps, you create the account and hold the secrets.

### 1. The machine

1. Create an Oracle Cloud account and an **Always Free** `VM.Standard.A1.Flex`
   instance — 2 OCPU, 12 GB, Ubuntu 22.04 or 24.04. Pick the region nearest you.
   If you get "out of capacity", try another availability domain or region.
2. In the VM's security list / network security group, open **80 and 443** only.
   Leave 8000 closed: Caddy reaches the API over the internal compose network as
   `api:8000`, never the published port.
3. Ubuntu images also carry a host firewall. Allow the same two ports:
   ```bash
   sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
   sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```

### 2. A free hostname, so HTTPS works

Caddy issues a real certificate automatically, but Let's Encrypt will not issue one
for a bare IP address — and `RISKRADAR_ENV=production` turns on **secure cookies**,
which a plain-HTTP site cannot send. So you need a hostname. Both of these are free:

* **sslip.io** — nothing to register. If the VM's IP is `129.1.2.3`, then
  `riskradar.129-1-2-3.sslip.io` already resolves to it.
* **DuckDNS** — free account, pick a name, point it at the IP. Nicer to read and it
  survives the IP changing.

### 3. The stack

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker

git clone <your repo URL> riskradar && cd riskradar
cp .env.example .env
```

Edit `.env` and set, at minimum:

```
RISKRADAR_ENV=production
RISKRADAR_SITE_ADDRESS=riskradar.129-1-2-3.sslip.io   # your hostname
RISKRADAR_ACME_EMAIL=you@example.com
RISKRADAR_HMAC_PEPPER=<long random string — changing it later orphans every baseline>
RISKRADAR_PG_SUPERUSER_PASSWORD=<secret>
RISKRADAR_APP_DB_PASSWORD=<secret>
RISKRADAR_MIGRATE_DB_PASSWORD=<secret>
RISKRADAR_CORS_ORIGINS=                                # stays empty: one origin, D101b
```

Then bring it up:

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.prod.yml \
  --env-file .env up -d --build
```

### 4. The D101c checklist — not optional once it is public

From `deploy/README.md`:

1. `python scripts/migrate.py`, then `ALTER ROLE riskradar_app PASSWORD ...` and the
   same for `riskradar_migrate`, to match `.env`.
2. `python scripts/seed.py` — note it creates a **public development API key**.
3. Create the real key (`POST /v1/admin/api-keys?name=switch`) and **revoke the
   development one** (`DELETE /v1/admin/api-keys/{id}`). In production, readiness
   stays failed until you do.
4. Promote a model and derive thresholds from the seeded traffic.
5. Give it data: run the simulator against the public address, or restore a snapshot.
   **Synthetic traffic only — D101a.** Never ingest real transaction data.

Check `https://<your hostname>/health/ready` returns 200 before you trust it.

### 5. Retire the tunnel

Once this is up, `scripts/share.py` and the `vercel.json` re-pointing are no longer
needed — Caddy serves the console and the API from one origin, which is what D101b
asked for. Either point the Vercel site at the VM's stable hostname once and leave
it, or drop Vercel and hand out the VM address directly.

## What this does not change

D26 is still LOCKED, as amended by D101: **the local path stays the rehearsed
primary for the assessed demo.** D26's reasoning — that a cloud deployment is a
second thing to fail on the day — is not weakened by this being free rather than
paid. Rehearse on `scripts/up.py`. The cloud instance is the shareable surface, not
the thing you stand in front of the examiners and depend on.

## If Oracle will not have you

There is no longer a completely free option that is always on, runs Postgres and
holds data for five weeks. Be honest about the choice at that point:

* **Keep the tunnel** for sharing, rehearse locally, and accept the laptop
  dependency — D26 says that is the sound plan anyway.
* **Pay about $4 a month** for a small VM (Hetzner, Vultr, DigitalOcean) and run
  exactly these steps on it. Everything above applies unchanged.
