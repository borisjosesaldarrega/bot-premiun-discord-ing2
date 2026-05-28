#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

MODE="${1:-start}"

export PYTHONUNBUFFERED=1
export DENO_INSTALL="$PWD/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"
# Preferimos FFmpeg nativo de Render. El binario de imageio-ffmpeg puede crashear con code -11.
export PREFER_SYSTEM_FFMPEG="${PREFER_SYSTEM_FFMPEG:-true}"

install_deno() {
  echo "🦕 Revisando Deno..."

  if command -v deno >/dev/null 2>&1; then
    echo "✅ Deno ya existe: $(deno --version | head -n 1)"
    return
  fi

  if [ -x "$DENO_INSTALL/bin/deno" ]; then
    echo "✅ Deno local ya existe: $DENO_INSTALL/bin/deno"
    return
  fi

  echo "📦 Instalando Deno local..."
  curl -fsSL https://deno.land/install.sh | sh

  if [ ! -x "$DENO_INSTALL/bin/deno" ]; then
    echo "❌ No se pudo instalar Deno."
    exit 1
  fi

  echo "✅ Deno instalado: $("$DENO_INSTALL/bin/deno" --version | head -n 1)"
}

install_python_deps() {
  echo "📦 Python actual: $(python --version)"

  if ! python - <<'PY'
import sys
raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)
PY
  then
    echo "⚠️ Render NO está usando Python 3.13.x."
    echo "   Sube .python-version con 3.13.13 y en Environment pon PYTHON_VERSION=3.13.13."
    echo "   Después usa Clear build cache & deploy."
  fi

  echo "📦 Actualizando pip..."
  python -m pip install --upgrade pip setuptools wheel

  echo "📦 Instalando requirements..."
  python -m pip install -r requirements.txt

  echo "🎬 Instalando imageio-ffmpeg solo como respaldo, NO como principal..."
  python -m pip install -U imageio-ffmpeg
}

setup_ffmpeg_path() {
  echo "🎬 Configurando FFmpeg..."

  if [ "${PREFER_SYSTEM_FFMPEG:-true}" = "true" ] && command -v ffmpeg >/dev/null 2>&1; then
    export FFMPEG_PATH="$(command -v ffmpeg)"
    echo "✅ Usando FFmpeg nativo de Render: $FFMPEG_PATH"
    ffmpeg -version | head -n 1 || true
    return
  fi

  export FFMPEG_PATH="$(
python - <<'PY'
try:
    import imageio_ffmpeg
    print(imageio_ffmpeg.get_ffmpeg_exe())
except Exception:
    print('ffmpeg')
PY
)"

  echo "⚠️ Usando FFmpeg de fallback: $FFMPEG_PATH"
  "$FFMPEG_PATH" -version | head -n 1 || true
}

prepare_runtime() {
  mkdir -p data/history data/playlists

  install_deno
  setup_ffmpeg_path

  echo "🔎 Verificaciones:"
  echo "Python: $(python --version)"
  echo "Deno: $(command -v deno || echo 'NO encontrado')"
  echo "FFmpeg: $FFMPEG_PATH"
  echo "PREFER_SYSTEM_FFMPEG=$PREFER_SYSTEM_FFMPEG"
}

if [ "$MODE" = "build" ]; then
  echo "🏗️ Build de Archeon para Render..."
  install_python_deps
  install_deno
  setup_ffmpeg_path
  echo "✅ Build terminado."
  exit 0
fi

if [ "$MODE" = "local" ]; then
  echo "💻 Iniciando Archeon en local/VPS..."

  PYTHON_BIN="${PYTHON_BIN:-python3}"

  if [ ! -d ".venv" ]; then
    echo "📦 Creando entorno virtual..."
    "$PYTHON_BIN" -m venv .venv
  fi

  source .venv/bin/activate
  install_python_deps

  if [ ! -f ".env" ]; then
    echo "⚠️ No existe .env. Copia .env.example a .env y pega tus llaves."
    exit 1
  fi

  prepare_runtime
  python bot.py
  exit 0
fi

echo "🚀 Iniciando Archeon..."
prepare_runtime
python bot.py
