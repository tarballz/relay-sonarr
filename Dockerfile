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

# Install backend deps first for layer caching.
COPY backend/pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" \
    "pydantic>=2.7" "pydantic-settings>=2.3" "pyyaml>=6.0" \
    "python-jose[cryptography]>=3.3"

COPY backend/ ./
# Drop the built SPA where main.py expects it (app/static).
COPY --from=frontend /fe/dist ./app/static

EXPOSE 8000
ENV CONFIG_PATH=/config/config.yaml
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
