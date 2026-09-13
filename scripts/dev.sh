#!/usr/bin/env bash
# 개발 서버: 백엔드(:8010, reload) + 프론트엔드(:5180, /api 프록시)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/uvicorn ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements-server.txt; }
[ -d web/node_modules ] || (cd web && npm install)
.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8010 --reload &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT
cd web && npm run dev
