# --- Stage 1: build the React SPA -------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- Stage 2: python runtime serving API + built SPA ------------------------
FROM python:3.11-slim AS runtime
WORKDIR /app

# Install backend deps first for layer caching. pyproject.toml is the single
# source of truth: read its dependency list rather than repeating it here.
COPY backend/pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY backend/ ./
# Drop the built SPA where main.py expects it (app/static).
COPY --from=frontend /fe/dist ./app/static

EXPOSE 8000
ENV CONFIG_PATH=/config/config.yaml
# Durable state (operations log, placements, availability cache) lives here.
# Mount a writable volume at /data (see docker-compose.yml).
ENV DATA_PATH=/data/relay.db

# Liveness: /healthz returns 503 when the reconciler is enabled but has gone
# stale, so a wedged loop (not just a dead port) is detectable. Uses stdlib so
# we don't need curl in the slim image. start-period covers first-tick warmup.
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]
