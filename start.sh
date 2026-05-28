#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "${RENDER:-}" = "true" ]; then
  echo "🚀 Iniciando Archeon en Render..."
  python bot.py
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -d ".venv" ]; then
  echo "📦 Creando entorno virtual..."
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

if [ ! -f ".env" ]; then
  echo "⚠️  No existe .env. Copia .env.example a .env y pega tus llaves."
  exit 1
fi

mkdir -p data/history data/playlists
python bot.py
