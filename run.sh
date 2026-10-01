#!/usr/bin/env bash
# Start Codex GUI on http://127.0.0.1:8765 (creates .venv on first run).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c "import fastapi, uvicorn, jinja2" 2>/dev/null; then
  .venv/bin/python -m pip install -q -r requirements.txt
fi

HOST="${CODEX_GUI_HOST:-127.0.0.1}"
PORT="${CODEX_GUI_PORT:-8765}"
echo "Codex GUI: http://${HOST}:${PORT}"
exec .venv/bin/python -m uvicorn --factory app.main:create_app --host "$HOST" --port "$PORT"
