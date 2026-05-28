#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

MODE="${1:-start}"

export PYTHONUNBUFFERED=1
export PIP_NO_CACHE_DIR=1
export PREFER_SYSTEM_FFMPEG="${PREFER_SYSTEM_FFMPEG:-true}"

# Deno local dentro del proyecto
export DENO_INSTALL="$PWD/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

install_python_deps() {
  echo "📦 Actualizando pip..."
  python -m pip install --upgrade pip setuptools wheel

  echo "📦 Instalando requirements..."
  python -m pip install -r requirements.txt

  echo "🎬 Instalando respaldo imageio-ffmpeg..."
  python -m pip install -U imageio-ffmpeg
}

install_deno() {
  echo "🦕 Revisando Deno..."

  if command -v deno >/dev/null 2>&1; then
    echo "✅ Deno encontrado: $(deno --version | head -n 1)"
    return 0
  fi

  if [ -x "$DENO_INSTALL/bin/deno" ]; then
    export PATH="$DENO_INSTALL/bin:$PATH"
    echo "✅ Deno local encontrado: $DENO_INSTALL/bin/deno"
    "$DENO_INSTALL/bin/deno" --version | head -n 1 || true
    return 0
  fi

  echo "📦 Instalando Deno local..."
  curl -fsSL https://deno.land/install.sh | sh

  export PATH="$DENO_INSTALL/bin:$PATH"

  if ! command -v deno >/dev/null 2>&1; then
    echo "❌ No se pudo instalar Deno."
    exit 1
  fi

  echo "✅ Deno instalado: $(deno --version | head -n 1)"
}

setup_ffmpeg_path() {
  echo "🎬 Configurando FFmpeg..."

  SYSTEM_FFMPEG="$(command -v ffmpeg || true)"

  if [ -n "$SYSTEM_FFMPEG" ]; then
    export FFMPEG_PATH="$SYSTEM_FFMPEG"
    export PREFER_SYSTEM_FFMPEG=true
    echo "✅ Usando FFmpeg del sistema: $FFMPEG_PATH"
    "$FFMPEG_PATH" -version | head -n 1 || true
    return 0
  fi

  echo "⚠️ No encontré ffmpeg del sistema. Usaré imageio-ffmpeg como respaldo."

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
}

prepare_cookies() {
  echo "🍪 Preparando cookies para yt-dlp..."

  mkdir -p /tmp/archeon_ytdlp
  COOKIE_TARGET="/tmp/archeon_ytdlp/cookies.txt"

  COOKIE_SOURCE=""

  # Si ya viene una variable personalizada, la respetamos primero
  if [ -n "${YTDLP_COOKIES_FILE:-}" ] && [ -f "${YTDLP_COOKIES_FILE:-}" ]; then
    COOKIE_SOURCE="$YTDLP_COOKIES_FILE"
  elif [ -n "${COOKIES_FILE:-}" ] && [ -f "${COOKIES_FILE:-}" ]; then
    COOKIE_SOURCE="$COOKIES_FILE"
  elif [ -f "/etc/secrets/cookies.txt" ]; then
    COOKIE_SOURCE="/etc/secrets/cookies.txt"
  elif [ -f "cookies.txt" ]; then
    COOKIE_SOURCE="cookies.txt"
  fi

  if [ -z "$COOKIE_SOURCE" ]; then
    echo "⚠️ No se encontró cookies.txt. YouTube puede bloquear algunas canciones."
    unset YTDLP_COOKIES_FILE || true
    unset COOKIES_FILE || true
    return 0
  fi

  if [ "$COOKIE_SOURCE" != "$COOKIE_TARGET" ]; then
    cp "$COOKIE_SOURCE" "$COOKIE_TARGET"
  fi

  chmod 600 "$COOKIE_TARGET" || true

  # Validación rápida para no usar un ejemplo vacío
  if ! grep -qiE "youtube\.com|googlevideo\.com" "$COOKIE_TARGET"; then
    echo "⚠️ El archivo de cookies existe, pero no parece tener cookies de YouTube."
    echo "⚠️ Fuente usada: $COOKIE_SOURCE"
  fi

  export YTDLP_COOKIES_FILE="$COOKIE_TARGET"
  export COOKIES_FILE="$COOKIE_TARGET"

  echo "✅ Cookies copiadas a ruta escribible: $YTDLP_COOKIES_FILE"
}

prepare_runtime() {
  echo "🧰 Preparando runtime de Archeon..."

  mkdir -p data/history data/playlists
  mkdir -p /tmp/archeon_ytdlp/cache

  export XDG_CACHE_HOME="/tmp/archeon_ytdlp/cache"

  install_deno
  setup_ffmpeg_path
  prepare_cookies

  echo "🔎 Verificaciones:"
  echo "Python: $(python --version)"
  echo "Deno: $(command -v deno || echo 'NO encontrado')"
  echo "FFmpeg: ${FFMPEG_PATH:-NO configurado}"
  echo "Cookies: ${YTDLP_COOKIES_FILE:-NO configuradas}"
  echo "Modo: $MODE"
}

if [ "$MODE" = "build" ]; then
  echo "🏗️ Build de Archeon para Render..."
  install_python_deps
  install_deno
  setup_ffmpeg_path
  prepare_cookies
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
  echo "🚀 Iniciando Archeon en local..."
  python bot.py
  exit 0
fi

echo "🚀 Iniciando Archeon en Render/VPS..."
prepare_runtime
python bot.py
