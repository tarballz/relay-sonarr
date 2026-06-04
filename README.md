# Relay — Unified Sonarr Dashboard

One web app over your two Sonarr instances (1080p `:8989` + 4K `:8990`). Search &
add shows (routing to either tier or both), monitor the combined queue, browse a
merged library, and a **smart fallback chain**: if a show has no qualifying release
on the 4K tier, it walks an ordered chain (`4K → 1080p → 1080p/SD`), prompting at
each step. Each step is an `(instance, profile)` pair — consecutive steps on the
same instance swap the quality profile instead of moving the series.

> The `SD` step requires an `SD` quality profile on the 1080p instance (allowing
> 720p/SD). Create it in Sonarr → Settings → Profiles, or that step reports
> "profile 'SD' not found". Chains are defined in `config.yaml` under
> `fallback_chains`.

- **Backend:** FastAPI + httpx (async), one `SonarrClient` per instance, fan-out
  aggregation. Serves the API and the built SPA on a single origin.
- **Frontend:** React + Vite (color-coded tiers: teal = 1080p, amber = 4K).
- **Deploy:** one container; exposed via your Cloudflare tunnel behind Cloudflare Access.

## 1. Configure

```bash
cp .env.example .env            # paste each Sonarr API key (Settings → General → API Key)
cp config.example.yaml config.yaml
# edit config.yaml — set each instance's URL and tweak the fallback chain
```

`config.yaml` and `.env` hold your real hosts/keys and are gitignored — edit the
copies, not the `.example` templates. `config.yaml` defines the instances (the
example points at `192.168.1.10:8989`/`:8990`) and the `4k → 1080p → SD` smart-add
fallback chain.

## 2. Run

```bash
docker compose up -d --build
```

Open `http://192.168.1.50:8088` (this box, `your-host`). The Settings page should
show both instances **online**. (Host port `8088` → container `8000`; change in
`docker-compose.yml`.)

> Note: `192.168.1.50` is **this** host; `192.168.1.10` in `config.yaml` is the
> separate box running Sonarr — don't confuse the two.

## 3. Expose via Cloudflare (Access-protected)

You already run `cloudflared`. Add an ingress hostname pointing at this container
and protect it with Cloudflare Access (edge login) — see `DEPLOY.md` for the exact
tunnel + Access steps. Optionally set `CF_ACCESS_ENABLED=true` (+ team domain and
AUD) in `.env` for in-app JWT verification (defense-in-depth).

## Development

```bash
# backend (terminal 1)
cd backend && uv run uvicorn app.main:app --reload   # CONFIG_PATH=../config.yaml
# frontend (terminal 2) — needs Node locally; proxies /api to :8000
cd frontend && npm install && npm run dev
```

## Tests

```bash
cd backend && uv run pytest         # 35 tests: client, services, availability, API
```

## API surface

`GET /api/instances` · `/search?term=` · `/series` · `/queue` ·
`/instances/{id}/profiles` · `/instances/{id}/root-folders` · `/settings` ·
`POST /api/add` · `POST /api/smart-add` · `POST /api/advance-fallback` ·
`GET /api/availability?instanceId=&seriesId=`
