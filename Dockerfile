FROM node:22-bookworm-slim AS web-build
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CAMHAL_DB_PATH=/var/lib/camhal/runs.sqlite3
WORKDIR /app
COPY pyproject.toml requirements-server.lock ./
COPY camhal_sim/ camhal_sim/
COPY backend/ backend/
COPY scripts/backup_runs.py scripts/backup_runs.py
COPY --from=web-build /build/web/dist/ web/dist/
RUN pip install --no-cache-dir -r requirements-server.lock . \
    && groupadd --system --gid 10001 camhal \
    && useradd --system --uid 10001 --gid camhal camhal \
    && mkdir -p /var/lib/camhal \
    && chown camhal:camhal /var/lib/camhal
USER camhal
EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/api/ready', timeout=3)" || exit 1
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1", "--no-access-log"]
