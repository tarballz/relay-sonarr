# Exposing Relay via Cloudflare (Access-protected)

Your `cloudflared` runs with a `TUNNEL_token`, i.e. a **dashboard-managed tunnel**.
So hostnames and Access policies are configured in the Cloudflare Zero Trust
dashboard, not a local config file. Two steps: route a hostname to the container,
then gate it with Access.

## A. Route a public hostname to the container

The app listens on **this** host (`your-host`, `192.168.1.50`) at port **`8088`**.
Your `cloudflared` container runs on this same box, so it can reach the app at
`192.168.1.50:8088`. (`192.168.1.10` is the separate Sonarr box — not this app.)

1. Cloudflare **Zero Trust** dashboard → **Networks → Tunnels**.
2. Open the tunnel that `nextcloud_cloudflared` runs (the one whose token is in
   `~/containers/nextcloud/docker-compose.yml`).
3. **Public Hostname → Add a public hostname:**
   - **Subdomain/Domain:** e.g. `sonarr.example.com`
   - **Type:** `HTTP`
   - **URL:** `192.168.1.50:8088`
4. Save. `https://sonarr.example.com` now reaches Relay.

> Tip: keep the host port (`8088`) firewalled to the LAN so the tunnel is the only
> external path. With Access (below), that means edge login is unavoidable.

## B. Protect it with Cloudflare Access (edge login)

1. Zero Trust → **Access → Applications → Add an application → Self-hosted**.
2. **Application domain:** `sonarr.example.com` (match exactly).
3. **Policies → Add policy:** Action `Allow`, rule e.g.
   *Emails → `you@example.com`* (or your Google login).
4. Save. Visiting the hostname now requires Cloudflare login before the app loads.

## C. (Optional) Defense-in-depth: verify the Access JWT in-app

So a host/LAN request can't bypass Access by hitting `:8088` directly:

1. Zero Trust → Access → your application → copy the **Application Audience (AUD)** tag.
2. Note your team domain, e.g. `yourteam.cloudflareaccess.com`.
3. In `.env`:
   ```
   CF_ACCESS_ENABLED=true
   CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
   CF_ACCESS_AUD=<the AUD tag>
   ```
4. `docker compose up -d` to apply. The app now rejects any request lacking a
   valid `Cf-Access-Jwt-Assertion` (which Cloudflare injects after login).

## Verify

- Visit `https://sonarr.example.com` → Cloudflare login appears → then Relay loads.
- Settings page shows both instances **online**.
- Add a known 4K-scarce show to the **4K** tier → the smart-fallback banner offers 1080p.

## Operations

### Health monitoring

- **`GET /healthz`** returns the reconciler snapshot and responds **503** when the
  autonomous loop is *enabled but stale* (no completed tick within `interval × 2`),
  so a wedged loop is detectable — not just a dead port. The image and compose file
  both define a `HEALTHCHECK`, so `docker compose ps` shows `healthy`/`unhealthy`.
- **`GET /api/reconciler/status`** (and the **System health** card on the Operations
  page) report last-tick time, duration, actions, `consecutiveFailures`, and the last
  error. Watch this to confirm the loop is doing its job.
- Logs go to stdout — `docker compose logs -f sonarr-unified`. Set `LOG_LEVEL=DEBUG`
  in `.env` for more detail.
- If the app exits at startup with *"Relay failed to start — Missing environment
  variable…"*, a referenced `SONARR_*_API_KEY` (or config field) is unset in `.env`.

### Backups — the `./data` SQLite volume

`./data/relay.db` holds Relay's durable state (series intents/policies, per-episode
placement, availability cache, operation log). The Sonarr instances are the source of
truth for actual files, so a lost DB means the reconciler re-derives state on the next
ticks — but you lose policies and history. Back it up.

The DB runs in **WAL mode**, so `relay.db` is accompanied by `relay.db-wal` /
`relay.db-shm`. **Do not** copy just `relay.db` while the app is running — use SQLite's
online backup, which captures a consistent snapshot:

```bash
# Consistent hot backup (safe while running):
docker compose exec sonarr-unified \
  python -c "import sqlite3; sqlite3.connect('/data/relay.db').backup(sqlite3.connect('/data/relay.backup.db'))"
# Copy the snapshot off the host (e.g. into a dated archive):
cp ./data/relay.backup.db "./backups/relay-$(date +%F).db"
```

A nightly cron of the above is enough. **Restore:** stop the app
(`docker compose down`), replace `./data/relay.db` with the backup (and delete any
stale `relay.db-wal`/`-shm`), then `docker compose up -d`.

### Upgrades

`git pull && docker compose up -d --build`. The schema migrates forward in place on
startup (`app/db.py`); take a backup first (above) if you want a rollback point.
