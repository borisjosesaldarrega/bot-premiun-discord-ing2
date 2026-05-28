#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

MODE="${1:-start}"
TARGET_PYTHON_VERSION="${PYTHON_VERSION:-3.13.13}"

export PYTHONUNBUFFERED=1
export DENO_INSTALL="$PWD/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

show_python_info() {
  echo "🐍 Python actual: $(python --version 2>&1)"
  echo "🎯 Python recomendado para Render: $TARGET_PYTHON_VERSION"

  CURRENT_VERSION="$(python - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
)"

  if [ "$CURRENT_VERSION" != "$TARGET_PYTHON_VERSION" ]; then
    echo "⚠️ Render NO está usando $TARGET_PYTHON_VERSION; está usando $CURRENT_VERSION."
    echo "⚠️ Sube un archivo .python-version con $TARGET_PYTHON_VERSION y haz Clear build cache & deploy."
  fi
}

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

  echo "📦 Instalando Deno local en $DENO_INSTALL..."
  curl -fsSL https://deno.land/install.sh | sh

  if [ ! -x "$DENO_INSTALL/bin/deno" ]; then
    echo "❌ No se pudo instalar Deno."
    exit 1
  fi

  echo "✅ Deno instalado: $("$DENO_INSTALL/bin/deno" --version | head -n 1)"
}

install_python_deps() {
  echo "📦 Actualizando pip..."
  python -m pip install --upgrade pip setuptools wheel

  echo "📦 Instalando requirements..."
  python -m pip install -r requirements.txt

  echo "🎬 Instalando herramientas extra para música..."
  python -m pip install -U "yt-dlp[default]" yt-dlp-ejs imageio-ffmpeg
}

setup_ffmpeg_path() {
  echo "🎬 Configurando FFmpeg..."

  export FFMPEG_PATH="$(
python - <<'PY'
try:
    import imageio_ffmpeg
    print(imageio_ffmpeg.get_ffmpeg_exe())
except Exception:
    print("ffmpeg")
PY
)"

  echo "✅ FFMPEG_PATH=$FFMPEG_PATH"

  if [ -x "$FFMPEG_PATH" ]; then
    "$FFMPEG_PATH" -version | head -n 1 || true
  else
    echo "⚠️ FFmpeg no parece ejecutable en esa ruta; se intentará usar ffmpeg del sistema."
  fi
}

prepare_runtime() {
  mkdir -p data/history data/playlists
  show_python_info
  install_deno
  setup_ffmpeg_path

  echo "🔎 Verificaciones finales:"
  echo "Python: $(python --version 2>&1)"
  echo "Deno: $(command -v deno || echo 'NO encontrado')"
  echo "FFmpeg: $FFMPEG_PATH"
}

if [ "$MODE" = "build" ]; then
  echo "🏗️ Build de Archeon para Render..."
  show_python_info
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
