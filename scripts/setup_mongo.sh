#!/usr/bin/env bash
# Install and start MongoDB for gois persistent storage.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v mongod >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing MongoDB Community via Homebrew…"
    brew tap mongodb/brew >/dev/null 2>&1 || true
    brew install mongodb-community@7.0 || brew install mongodb-community
  else
    echo "MongoDB not found. Install manually: https://www.mongodb.com/docs/manual/installation/" >&2
    exit 1
  fi
fi

if command -v brew >/dev/null 2>&1; then
  brew services start mongodb-community@7.0 >/dev/null 2>&1 \
    || brew services start mongodb-community >/dev/null 2>&1 \
    || true
fi

if [[ -d .venv ]]; then
  .venv/bin/pip install -q 'pymongo>=4.0'
  echo "✓ Python pymongo installed in .venv"
fi

ENV_FILE="$ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
  if ! grep -q '^MONGODB_URI=' "$ENV_FILE" 2>/dev/null; then
    printf '\n# Primary app storage\nMONGODB_URI=mongodb://localhost:27017\nMONGODB_DB=gois\n' >> "$ENV_FILE"
    echo "✓ Added MONGODB_URI to .env"
  fi
else
  echo "Tip: copy .env.example to .env and set MONGODB_URI=mongodb://localhost:27017"
fi

# shellcheck source=lib/resolve-python.sh
source "${ROOT}/scripts/lib/resolve-python.sh"
PY="$(qclaw_resolve_python "$ROOT")"

for _ in $(seq 1 15); do
  if "$PY" -c "from gois.mongo import ping; raise SystemExit(0 if ping() else 1)" 2>/dev/null; then
    echo "✓ MongoDB is running"
    echo ""
    echo "Import all local stores:"
    echo "  $PY -m gois.scripts.migrate_all_to_mongo --config config.yaml --merge"
    exit 0
  fi
  sleep 1
done

echo "MongoDB installed but not reachable yet. Try: brew services restart mongodb-community" >&2
exit 1
