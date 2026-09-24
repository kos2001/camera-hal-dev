#!/usr/bin/env bash
# 단일 서버: 프론트엔드를 빌드해 백엔드(:8010)가 함께 서빙
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements-server.lock
(cd web && npm ci --cache .npm-cache && npm run build)
exec .venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port "${PORT:-8010}" --no-access-log
