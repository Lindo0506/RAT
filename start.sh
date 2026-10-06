#!/usr/bin/env bash
# Repo Analysis Tool (RAT) - one-command startup script.
#
# Usage:
#   ./start.sh              # serve the dashboard on http://localhost:8000
#   PORT=9000 ./start.sh    # serve on a custom port
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: python3 is required but was not found on PATH" >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "error: git is required but was not found on PATH" >&2
    exit 1
fi

# Create an isolated virtualenv on first run (reused afterwards).
if [ ! -x ".venv/bin/python" ]; then
    echo "==> Creating virtual environment (.venv)..."
    "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

PORT="${PORT:-8000}"
echo "==> Starting RAT dashboard on http://localhost:${PORT}"
exec python run.py --host 0.0.0.0 --port "$PORT"
