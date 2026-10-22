#!/usr/bin/env bash
# Usage: ./run.sh   (loads .env if present)
set -e
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
pip install -q -r requirements.txt
cd backend && exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
