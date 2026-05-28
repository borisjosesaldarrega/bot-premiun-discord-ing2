import asyncio
import base64
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import shutil
import importlib.metadata as importlib_metadata
import random
import subprocess
import re
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from functools import partial
from typing import Optional, Dict, List, Union, Set
import aiohttp
import yt_dlp as youtube_dl
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
import google.generativeai as genai
import discord
from discord import app_commands, Embed
from discord.ext import commands
from encodings.aliases import aliases

# --------------------------
# Configuración inicial
# --------------------------
# Cargar variables de entorno
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("discord_token")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
STABILITY_API_KEY = os.getenv("STABILITY_API_KEY")

# Configuración de logging con rotación para que bot.log no crezca sin control
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('bot.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class QuietYDLLogger:
    """Silencia errores internos repetidos de yt-dlp para que Render no parezca incendio.

    El bot sigue guardando un resumen propio en logger.warning cuando una búsqueda falla,
    pero evita spamear cientos de líneas tipo "Requested format is not available".
    """
    def debug(self, msg):
        pass

    def warning(self, msg):
        text = str(msg)
        if "Requested format is not available" not in text:
            logger.debug(f"yt-dlp warning: {text[:250]}")

    def error(self, msg):
        text = str(msg)
        # Lo dejamos en debug porque resolve_song ya resume el error de forma controlada.
        logger.debug(f"yt-dlp error: {text[:250]}")

# Configurar el nivel de logging para librerías específicas
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('google').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# --------------------------
# Diagnóstico de voz / DAVE
# --------------------------
VOICE_4017_HINT = (
    "Discord cerró la conexión de voz con código 4017. "
    "Desde la actualización de voz/E2EE de Discord, esto casi siempre significa que tu entorno no tiene soporte DAVE actualizado. "
    "Ejecuta en ESTA MISMA .venv:\n"
    "python -m pip install -U \"discord.py[voice]\" davey PyNaCl\n"
    "python -m discord --version\n"
    "Después reinicia el bot."
)

def _pkg_version(package_name: str) -> str:
    """Devuelve versión instalada o 'NO instalado' sin romper el arranque."""
    try:
        return importlib_metadata.version(package_name)
    except importlib_metadata.PackageNotFoundError:
        return "NO instalado"
    except Exception:
        return "desconocida"

def log_voice_dependency_status() -> None:
    """Muestra en consola lo importante para que la voz funcione."""
    try:
        logger.info(
            "Entorno de voz: Python %s | discord.py %s | PyNaCl %s | davey %s",
            sys.version.split()[0],
            getattr(discord, "__version__", "desconocida"),
            _pkg_version("PyNaCl"),
            _pkg_version("davey")
        )

        if _pkg_version("davey") == "NO instalado":
            logger.warning(
                "davey no está instalado. La voz de Discord puede fallar con código 4017. "
                "Instala con: python -m pip install -U \"discord.py[voice]\" davey PyNaCl"
            )
    except Exception as e:
        logger.warning(f"No pude revisar dependencias de voz: {e}")

def is_voice_4017_error(error: BaseException) -> bool:
    """Detecta el cierre 4017 aunque venga envuelto por discord.py."""
    code = getattr(error, "code", None)
    close_code = getattr(error, "_close_code", None)
    text_error = str(error)
    return code == 4017 or close_code == 4017 or "4017" in text_error

def human_voice_error(error: BaseException) -> str:
    """Mensaje corto para Discord cuando falla la voz."""
    if is_voice_4017_error(error):
        return (
            "❌ No pude conectarme al canal de voz.\n\n"
            "**Causa:** Discord cerró la voz con código `4017`.\n"
            "**Solución:** actualiza las dependencias de voz en la misma `.venv`:\n"
            "```powershell\n"
            "python -m pip install -U \"discord.py[voice]\" davey PyNaCl\n"
            "python -m discord --version\n"
            "```\n"
            "Luego reinicia el bot. Es Discord pidiendo papeles nuevos, no tu bot haciéndose el dramático."
        )

    return f"⚠️ Error al conectar a voz: `{str(error)[:500]}`"


# Configuración de IA. Si falta la llave, el bot arranca igual y solo fallan los comandos de IA.
model = None
if GOOGLE_API_KEY:
    genai.configure(
        api_key=GOOGLE_API_KEY,
        transport='rest'
    )

    # Creación del modelo con configuración optimizada
    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        generation_config={
            "temperature": 0.7,  # Balance entre creatividad y coherencia
            "top_p": 0.9,
            "top_k": 40,
            "max_output_tokens": 1500  # Límite razonable para respuestas
        },
        safety_settings={
            category: "BLOCK_NONE" for category in [
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT"
            ]
        },
        system_instruction="Eres un asistente de Discord llamado Archeon. Sé conciso y útil."
    )
else:
    logger.warning("GOOGLE_API_KEY no está configurada. Los comandos de IA quedarán desactivados.")

# Configuración del bot
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix=commands.when_mentioned_or('¡'), intents=intents, help_command=None, heartbeat_timeout=60.0, guild_ready_timeout=10, member_cache_flags=discord.MemberCacheFlags.none(), chunk_guilds_at_startup=False)


# --------------------------
# Tareas asyncio seguras
# --------------------------

def _log_task_exception(task: asyncio.Task) -> None:
    """Evita el warning 'Task exception was never retrieved' y deja el error real en logs."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.error("No pude leer la excepción de una tarea asyncio:\n%s", traceback.format_exc())
        return

    if exc:
        task_name = task.get_name() if hasattr(task, "get_name") else "tarea_asyncio"
        logger.error("Error en tarea asyncio '%s': %s\n%s", task_name, repr(exc), ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def create_logged_task(coro, name: Optional[str] = None) -> asyncio.Task:
    """Crea tareas en segundo plano con callback de errores.

    Sin esto, Python muestra 'Task exception was never retrieved' cuando una tarea creada
    con create_task falla y nadie consulta su excepción.
    """
    try:
        task = bot.loop.create_task(coro, name=name)
    except TypeError:
        task = bot.loop.create_task(coro)

    task.add_done_callback(_log_task_exception)
    return task
log_voice_dependency_status()
MODERATION_DATA_PATH = "data/moderation_data.json"
os.makedirs("data", exist_ok=True)


# --------------------------
# Constantes y variables globales
# --------------------------

URL_REGEX = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')
MAX_HISTORY = 10
IDLE_TIMEOUT = 300  # 5 minutos
INACTIVITY_TIMEOUT = 1800

# Estructuras de datos
chat_histories: Dict[str, List[str]] = {}
saved_playlists: Dict[int, Dict[str, List[Dict]]] = {}
queues: Dict[int, List[Dict]] = {}
current_songs: Dict[int, Dict] = {}  # canción actual por servidor
loop_mode: Dict[int, bool] = {}
last_activity: Dict[int, float] = {}
bypass_messages = set()
dj_sessions = {}
auto_add_lock = asyncio.Lock()
voice_connection_attempts: Dict[int, float] = {}  # evita limpiar cola durante fallos de handshake de voz

# Voz estable: guarda el último canal y evita que el bot se baje solo mientras hay música.
last_voice_channel_ids: Dict[int, int] = {}
last_text_channel_ids: Dict[int, int] = {}  # último canal de texto útil para avisos de voz
voice_stay_connected_guilds: Set[int] = set()

# Por defecto NO se autodesconecta. Si quieres activar autodesconexión por soledad:
# AUTO_VOICE_DISCONNECT=true en .env
AUTO_VOICE_DISCONNECT_ENABLED = os.getenv("AUTO_VOICE_DISCONNECT", "true").lower() in {"1", "true", "yes", "on"}

# Moderación con IA apagada por defecto para no quemar la cuota de Gemini con cada mensaje.
# Si quieres que Gemini analice mensajes, pon MODERATION_AI_ENABLED=true en .env.
MODERATION_AI_ENABLED = os.getenv("MODERATION_AI_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
gemini_moderation_cooldown_until: float = 0.0

# El modo DJ NO depende de Gemini por defecto para evitar errores 429 de cuota.
# Si luego quieres que Gemini ayude a sugerir canciones, pon DJ_USE_GEMINI=true en .env.
DJ_USE_GEMINI = os.getenv("DJ_USE_GEMINI", "false").lower() in {"1", "true", "yes", "on"}
gemini_dj_cooldown_until: float = 0.0

# Render/hosting cloud: YouTube puede devolver solo formatos SABR o sin audio.
# En ese caso no queremos spamear logs ni poner cualquier remix; preferimos fallar claro.
RUNNING_ON_RENDER = bool(os.getenv('RENDER') or os.getenv('RENDER_EXTERNAL_URL') or os.getenv('RENDER_SERVICE_ID'))
STRICT_MUSIC_MATCH = os.getenv('STRICT_MUSIC_MATCH', 'true').lower() in {'1', 'true', 'yes', 'on'}


# --------------------------
# Configuración de tickets y bienvenida
# --------------------------

def env_int(name: str, default: int = 0) -> int:
    """Lee un entero desde .env sin romper el bot si está vacío o mal escrito."""
    try:
        value = os.getenv(name, "").strip()
        return int(value) if value else default
    except Exception:
        logger.warning(f"Variable {name} inválida. Usando {default}.")
        return default


def env_int_list(name: str, default: Optional[List[int]] = None) -> List[int]:
    """Lee una lista de IDs separados por coma desde .env."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default or [])

    ids: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


# Canal donde el usuario abre tickets o pulsa el botón.
# En tu servidor corresponde al canal #tickets.
TICKET_REQUEST_CHANNEL_ID = env_int("TICKET_REQUEST_CHANNEL_ID", 1387966992811556944)

# Canal interno donde se guardan registros/logs. Si no configuras otro, usa el mismo.
TICKET_LOG_CHANNEL_ID = env_int("TICKET_LOG_CHANNEL_ID", TICKET_REQUEST_CHANNEL_ID)

# Roles que pueden ver y responder tickets privados. Deben ser IDs de ROL, no IDs de usuario.
TICKET_STAFF_ROLE_IDS = env_int_list("TICKET_STAFF_ROLE_IDS", env_int_list("ADMIN_ROLE_IDS", []))

# Canal de bienvenida.
WELCOME_CHANNEL_ID = env_int("WELCOME_CHANNEL_ID", 902057453204697092)

TICKET_CATEGORY_NAME = os.getenv("TICKET_CATEGORY_NAME", "🎫 Tickets privados")
TICKET_PANEL_ENABLED = os.getenv("TICKET_PANEL_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
WELCOME_ENABLED = os.getenv("WELCOME_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

# Evita que un usuario abra veinte tickets seguidos por accidente.
ticket_cooldowns: Dict[int, float] = {}

# --------------------------
# Auto-desconexión de voz
# --------------------------
# Activo por defecto: si el bot queda solo o nadie lo usa, se desconecta y avisa.
# Puedes ajustar estos tiempos desde .env sin tocar código.
VOICE_ALONE_TIMEOUT = env_int("VOICE_ALONE_TIMEOUT", 180)  # segundos solo en llamada
VOICE_IDLE_TIMEOUT = env_int("VOICE_IDLE_TIMEOUT", 600)    # segundos sin uso con gente en llamada
VOICE_CHECK_INTERVAL = max(15, env_int("VOICE_CHECK_INTERVAL", 30))

# Memoria del DJ para evitar que repita la misma canción como disco rayado.
DJ_RECENT_MEMORY = int(os.getenv("DJ_RECENT_MEMORY", "30"))
dj_recent_markers: Dict[int, List[str]] = {}
dj_auto_tasks: Dict[int, asyncio.Task] = {}


# Configuración de FFmpeg
# Para Discord suele ser más estable enviar PCM y dejar que discord.py/PyNaCl lo codifique.
# La versión anterior usaba FFmpegOpusAudio con parámetros muy agresivos de análisis; en algunos equipos
# eso conectaba pero no se escuchaba. Esta configuración prioriza que suene estable.
FFMPEG_BEFORE_OPTIONS = (
    '-reconnect 1 '
    '-reconnect_streamed 1 '
    '-reconnect_delay_max 10 '
    '-nostdin '
    '-hide_banner '
    '-loglevel warning '
    '-user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36" '
)

FFMPEG_AUDIO_OPTIONS = (
    '-vn '
    '-bufsize 1024k '
    '-ac 2 '
    '-ar 48000 '
)

FFMPEG_EXECUTABLE = os.getenv("FFMPEG_PATH", "ffmpeg")
DEFAULT_GUILD_VOLUME = 0.90
guild_volumes: Dict[int, float] = {}

FFMPEG_OPTIONS = {
    'before_options': FFMPEG_BEFORE_OPTIONS,
    'options': FFMPEG_AUDIO_OPTIONS,
    'executable': FFMPEG_EXECUTABLE,
    # No pasamos subprocess.PIPE: en Python 3.13 da warning y no aporta aquí.
}

# Opciones para yt-dlp.
# Restauradas con la lógica del bot 18 porque en Render era la que sí encontraba música rápido.
# La lógica nueva de estrategias múltiples estaba saltando resultados válidos de YouTube.
def get_cookiefile_path() -> Optional[str]:
    """Devuelve cookies.txt local o el Secret File de Render si existe."""
    for cookie_path in ("cookies.txt", "/etc/secrets/cookies.txt"):
        if os.path.exists(cookie_path):
            return cookie_path
    return None


ydl_opts = {
    'format': 'bestaudio/best',
    'default_search': 'ytsearch',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'ignoreerrors': True,
    'extract_flat': False,
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'nocheckcertificate': True,
    'source_address': '0.0.0.0',
    # Mantengo también las keys antiguas porque el bot 18 funcionaba así.
    'geo-bypass': True,
    'geo_bypass': True,
    'no-cache-dir': True,
    'cachedir': False,
}

cookie_path = get_cookiefile_path()
if cookie_path:
    ydl_opts['cookiefile'] = cookie_path
    logger.info(f"Usando cookies de YouTube desde: {cookie_path}")
else:
    logger.warning("No se encontró cookies.txt. YouTube puede bloquear música en Render.")


# Opciones viejas de FFmpeg para música en Render.
# Sí, son menos elegantes que PCMVolumeTransformer, pero fueron las que ya te funcionaban.
LEGACY_FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 '
        '-reconnect_streamed 1 '
        '-reconnect_delay_max 10 '
        '-loglevel warning '
        '-user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36" '
        '-http_persistent 1 '
        '-multiple_requests 1 '
        '-fflags +discardcorrupt+fastseek '
        '-probesize 32 '
        '-analyzeduration 0 '
        '-protocol_whitelist file,pipe,udp,rtp,tcp,https,tls '
        '-hide_banner '
    ),
    'options': (
        '-vn '
        '-bufsize 512k '
        '-af dynaudnorm=p=0.5 '
        '-c:a libopus '
        '-application lowdelay '
        '-strict experimental '
    ),
    'executable': FFMPEG_EXECUTABLE,
    'stderr': subprocess.PIPE,
}


def _pick_audio_url_from_info(info: Dict) -> Optional[str]:
    """Saca una URL de audio usando la lógica simple del bot 18, pero sin reventar."""
    if not isinstance(info, dict):
        return None

    audio_url = info.get("url")
    if audio_url:
        return audio_url

    formats = info.get("formats") or []

    # Primero audio puro.
    for fmt in formats:
        if (
            isinstance(fmt, dict)
            and fmt.get("url")
            and fmt.get("acodec") not in (None, "none")
            and fmt.get("vcodec") in (None, "none")
        ):
            return fmt.get("url")

    # Luego cualquier formato que tenga audio.
    for fmt in formats:
        if isinstance(fmt, dict) and fmt.get("url") and fmt.get("acodec") not in (None, "none"):
            return fmt.get("url")

    # Último recurso: cualquier URL de formato.
    for fmt in formats:
        if isinstance(fmt, dict) and fmt.get("url"):
            return fmt.get("url")

    return None


def _normalize_yt_dlp_dump(info) -> List[Dict]:
    """Normaliza salida de yt-dlp/CLI a una lista de entradas dict."""
    if not info:
        return []
    if isinstance(info, dict) and "entries" in info:
        return [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    return [info] if isinstance(info, dict) else []


def get_deno_runtime_path() -> Optional[str]:
    """Busca Deno instalado por render_build.sh o en PATH."""
    candidates = [
        os.getenv("DENO_PATH"),
        os.path.join(os.getcwd(), ".deno", "bin", "deno"),
        "/opt/render/project/src/.deno/bin/deno",
        "/opt/render/project/.deno/bin/deno",
        shutil.which("deno"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


async def run_yt_dlp_cli_json(query: str, *, use_cookies: bool = False, search_count: int = 3):
    """Ejecuta yt-dlp por CLI con Deno/EJS.

    En Render, el error "Requested format is not available" suele venir de YouTube
    pidiendo resolver desafíos JavaScript. La API Python no siempre deja claro qué
    runtime está usando, así que este fallback llama a `python -m yt_dlp` con
    `--js-runtimes deno:...` y `--remote-components ejs:github`.
    """
    is_url = bool(URL_REGEX.match(query))
    search_value = query if is_url else f"ytsearch{max(1, min(search_count, 5))}:{query}"

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--ignore-errors",
        "--no-cache-dir",
        "--force-ipv4",
        "-f",
        "bestaudio/best",
        "--default-search",
        "ytsearch",
    ]

    deno_path = get_deno_runtime_path()
    if deno_path:
        cmd.extend(["--js-runtimes", f"deno:{deno_path}"])
        # GitHub evita depender de npm; funciona mejor en hosting cerrados.
        cmd.extend(["--remote-components", "ejs:github"])
    else:
        logger.warning("No encontré Deno. YouTube puede devolver solo imágenes/SABR en Render.")

    cookiefile = get_cookiefile_path()
    if use_cookies and cookiefile:
        cmd.extend(["--cookies", cookiefile])

    cmd.append(search_value)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise MusicSearchError("yt-dlp tardó demasiado resolviendo YouTube.")

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    if process.returncode != 0 and not out:
        raise MusicSearchError((err or f"yt-dlp CLI salió con código {process.returncode}")[:700])

    if not out:
        raise MusicSearchError((err or "yt-dlp CLI no devolvió JSON")[:700])

    # Por seguridad, si alguna advertencia se coló, buscamos el primer JSON.
    json_start = out.find("{")
    if json_start > 0:
        out = out[json_start:]

    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise MusicSearchError(f"yt-dlp CLI devolvió JSON inválido: {out[:250]}") from exc


async def extract_music_with_cli_ejs(query: str):
    """Extractor principal para Render: CLI + Deno/EJS + cookies como respaldo."""
    last_error = None

    # Primero sin cookies. Con YouTube actual, cookies válidas a veces provocan formatos vacíos.
    for use_cookies in (False, True):
        try:
            info = await run_yt_dlp_cli_json(query, use_cookies=use_cookies, search_count=3)
            entries = _normalize_yt_dlp_dump(info)
            if not entries:
                raise MusicSearchError("yt-dlp CLI no devolvió entradas.")

            for entry in entries:
                audio_url = _pick_audio_url_from_info(entry)
                if audio_url:
                    logger.info(
                        "Canción resuelta con CLI EJS (%s): %s",
                        "cookies" if use_cookies else "sin cookies",
                        str(entry.get("title") or query)[:100]
                    )
                    return entry, audio_url

            raise MusicSearchError("yt-dlp CLI encontró resultados, pero ninguno traía audio reproducible.")
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Extractor CLI EJS falló para '%s' (%s): %s",
                query,
                "cookies" if use_cookies else "sin cookies",
                str(exc)[:220]
            )

    raise MusicSearchError(str(last_error or "yt-dlp CLI no pudo resolver la canción"))


async def extract_music_with_python_api_bot18(query: str):
    """Extractor viejo del bot 18 usando la API Python de yt-dlp."""
    is_url = bool(URL_REGEX.match(query))
    search_value = query if is_url else f"ytsearch:{query}"

    # Igual que el bot 18, pero probando sin cookies y luego con cookies.
    for use_cookies in (False, True):
        opts = dict(ydl_opts)
        opts.pop("cookiefile", None)
        if use_cookies:
            cookiefile = get_cookiefile_path()
            if cookiefile:
                opts["cookiefile"] = cookiefile
            else:
                continue

        try:
            with youtube_dl.YoutubeDL(opts) as ydl:
                info = await extract_info_async(ydl, search_value, download=False)

            entries = _normalize_yt_dlp_dump(info)
            if not entries:
                raise MusicSearchError("yt-dlp no devolvió resultados válidos desde YouTube.")

            info = entries[0]
            audio_url = _pick_audio_url_from_info(info)
            if not audio_url:
                raise MusicSearchError("YouTube encontró el video, pero no entregó URL de audio reproducible.")

            logger.info(
                "Canción resuelta con API estilo bot 18 (%s): %s",
                "cookies" if use_cookies else "sin cookies",
                str(info.get("title") or query)[:100]
            )
            return info, audio_url
        except Exception as exc:
            logger.warning(
                "Extractor API bot18 falló para '%s' (%s): %s",
                query,
                "cookies" if use_cookies else "sin cookies",
                str(exc)[:220]
            )

    raise MusicSearchError("yt-dlp no devolvió audio reproducible desde YouTube.")


async def extract_music_legacy_like_bot18(query: str):
    """Extrae música priorizando que funcione en Render.

    Orden:
    1. CLI con Deno/EJS, que es el fix real para el error de formatos/SABR.
    2. API Python estilo bot 18, como respaldo.
    """
    try:
        return await extract_music_with_cli_ejs(query)
    except Exception as cli_error:
        logger.warning("CLI EJS no pudo resolver '%s'; pruebo API bot18: %s", query, str(cli_error)[:220])

    return await extract_music_with_python_api_bot18(query)


# --------------------------
# Funciones auxiliares
# --------------------------

def get_history_path(guild_id: int, date: Optional[str] = None) -> str:
    """Obtiene la ruta del archivo de historial para una fecha"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    os.makedirs("data/history", exist_ok=True)
    return f"data/history/{guild_id}_{date}.json"


def get_playlist_path(user_id: int, playlist_name: str) -> str:
    """Obtiene la ruta del archivo de playlist para un usuario"""
    os.makedirs("data/playlists", exist_ok=True)
    return f"data/playlists/{user_id}_{playlist_name.lower().replace(' ', '_')}.json"


def save_to_history(guild_id: int, song: Dict) -> None:
    """Guarda una canción en el historial del servidor"""
    date = datetime.now().strftime("%Y-%m-%d")
    history_file = get_history_path(guild_id, date)

    # Crear una copia del diccionario de la canción
    song_copy = song.copy()

    # Reemplazar el objeto Member con un diccionario que contenga solo la información necesaria
    if 'requested_by' in song_copy and hasattr(song_copy['requested_by'], 'id'):
        song_copy['requested_by'] = {
            'id': song_copy['requested_by'].id,
            'name': song_copy['requested_by'].display_name
        }

    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []

    if not any(s['url'] == song_copy['url'] for s in history):
        history.append(song_copy)
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

def load_history(guild_id: int, date: Optional[str] = None) -> List[Dict]:
    """Carga el historial de un servidor para una fecha específica"""
    history_file = get_history_path(guild_id, date)

    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def get_user_playlists(user_id: int) -> List[str]:
    """Obtiene todas las playlists de un usuario"""
    playlists = []
    prefix = f"{user_id}_"

    try:
        for filename in os.listdir('data/playlists'):
            if filename.startswith(prefix) and filename.endswith('.json'):
                playlist_name = filename[len(prefix):-5].replace('_', ' ')
                playlists.append(playlist_name)
    except FileNotFoundError:
        os.makedirs("data/playlists", exist_ok=True)

    return playlists


def save_playlist(user_id: int, playlist_name: str, songs: List[Dict]) -> int:
    """Guarda una playlist para un usuario"""
    playlist_file = get_playlist_path(user_id, playlist_name)

    # Eliminar duplicados manteniendo el orden
    seen_urls = set()
    unique_songs = []

    for song in songs:
        if song['url'] not in seen_urls:
            seen_urls.add(song['url'])
            unique_songs.append(song)

    with open(playlist_file, 'w', encoding='utf-8') as f:
        json.dump(unique_songs, f, ensure_ascii=False, indent=2)
    return len(unique_songs)


def load_playlist(user_id: int, playlist_name: str) -> Optional[List[Dict]]:
    """Carga una playlist de un usuario"""
    playlist_file = get_playlist_path(user_id, playlist_name)

    try:
        with open(playlist_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def check_same_voice_channel(ctx: commands.Context) -> bool:
    """Verifica si el usuario está en el mismo canal de voz que el bot"""
    if not ctx.author.voice or not ctx.author.voice.channel:
        return False

    voice_client = ctx.voice_client
    if not voice_client or not voice_client.is_connected():
        return True  # Permitir si el bot no está en ningún canal

    return ctx.author.voice.channel == voice_client.channel

#---------------------------------------------------------------
# Temas visuales del módulo DJ
#---------------------------------------------------------------

DJ_THEMES = {
    "default": {
        "icon": "🎧",
        "color": discord.Color.purple(),
        "name": "Modo DJ"
    },
    "party": {
        "icon": "🎉",
        "color": discord.Color.magenta(),
        "name": "Fiesta"
    },
    "chill": {
        "icon": "🌴",
        "color": discord.Color.blue(),
        "name": "Chill"
    },
    "workout": {
        "icon": "💪",
        "color": discord.Color.orange(),
        "name": "Entrenamiento"
    },
    "focus": {
        "icon": "🎯",
        "color": discord.Color.green(),
        "name": "Concentración"
    }
}


#---------------------------------------------------------------
# modulo para crear los embed consistente del modulo dj
#---------------------------------------------------------------

def create_dj_embed(title, description, theme="default", footer=None, thumbnail=None):
    theme_data = DJ_THEMES.get(theme, DJ_THEMES["default"])

    embed = discord.Embed(
        title=f"{theme_data['icon']} {title}",
        description=description,
        color=theme_data["color"]
    )

    if footer:
        embed.set_footer(text=footer)

    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    return embed

#--------------------------------------------------------------
# Modulo de moderaacion que guarda los datos
#--------------------------------------------------------------

def save_moderation_data():
    """Guarda los datos de moderación en un archivo JSON"""
    os.makedirs("data", exist_ok=True)
    data = {
        "user_warnings": user_warnings,
        "allowed_channels": {str(k): v for k, v in allowed_channels.items()},
        "malicious_domains": list(malicious_domains)
    }

    try:
        with open(MODERATION_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error al guardar datos de moderación: {e}")

def load_moderation_data():
    """Carga los datos de moderación desde un archivo JSON"""
    global user_warnings, allowed_channels, malicious_domains

    try:
        if os.path.exists(MODERATION_DATA_PATH):
            with open(MODERATION_DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

                # Convertir keys de allowed_channels de str a int
                allowed_channels = {int(k): v for k, v in data.get("allowed_channels", {}).items()}

                # Convertir user_warnings keys de str a int
                user_warnings = {int(k): v for k, v in data.get("user_warnings", {}).items()}

                malicious_domains.update(set(data.get("malicious_domains", [])))

    except Exception as e:
        logger.error(f"Error al cargar datos de moderación: {e}")
        # Inicializar estructuras vacías si hay error
        user_warnings = {}
        allowed_channels = {}
        malicious_domains = set()

# --------------------------
# Módulo de Música
# --------------------------

async def check_queue(ctx: Union[commands.Context, discord.Interaction]) -> None:
    """Reproduce la siguiente canción en la cola de forma segura por servidor."""
    guild = ctx if isinstance(ctx, discord.Guild) else getattr(ctx, 'guild', None)
    if not guild:
        return

    guild_id = guild.id
    voice_client = get_voice_client_from_context(ctx) or guild.voice_client

    if not voice_client or not voice_client.is_connected():
        current_songs.pop(guild_id, None)
        return

    if not queues.get(guild_id):
        current_songs.pop(guild_id, None)
        return

    next_song = queues[guild_id].pop(0)

    try:
        source = await create_audio_source(next_song['url'], guild_id)

        current_songs[guild_id] = next_song
        current_song = current_songs[guild_id]
        save_to_history(guild_id, current_song)
        if dj_sessions.get(guild_id, {}).get("active"):
            remember_dj_song(guild_id, current_song)

        voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(
                check_queue(ctx),
                bot.loop
            ) if e is None else logger.error(f'Error en reproducción: {e}')
        )

        embed = discord.Embed(
            title="🎵 Reproduciendo ahora (desde cola)",
            description=f"[{current_song['title']}]({current_song['web_url']})",
            color=discord.Color.blurple()
        )

        if current_song.get('duration', 0) > 0:
            mins, secs = divmod(current_song['duration'], 60)
            embed.add_field(name="Duración", value=f"{mins}:{secs:02d}")

        if current_song.get('thumbnail'):
            embed.set_thumbnail(url=current_song['thumbnail'])
        requested_by = current_song.get('requested_by')
        if hasattr(requested_by, 'display_name'):
            embed.set_footer(text=f"Solicitado por {requested_by.display_name}")

        await send_context_message(ctx, embed=embed)

    except Exception as e:
        logger.error(f"Error en check_queue: {traceback.format_exc()}")
        current_songs.pop(guild_id, None)
        await send_context_message(ctx, "⚠️ Error al pasar a la siguiente canción")


async def generate_goodbye_message(reason: str) -> str:
    """Genera un mensaje chistoso de despedida con la IA"""
    try:
        if model is None:
            return "Me desconecto... falta configurar la IA, pero igual me voy 😤"

        prompt = (
            f"Imagina que eres un bot de Discord algo sarcástico y cansado. "
            f"Te estás desconectando de un canal de voz porque {reason}. "
            f"Genera un mensaje de despedida gracioso y corto (menos de 20 palabras), como si fueras un gamer flojo o un bot vago. "
            f"Ejemplos: 'Me voy a jugar Genshin, esto está más muerto que mi GPU', 'Zzz... nadie me habla, bye', 'Me desconecto antes de oxidarme'."
        )
        respuesta = model.generate_content(prompt)
        return respuesta.text.strip()
    except Exception as e:
        logger.error(f"Error al generar mensaje de despedida: {e}")
        return "Me desconecto... no soy tu niñera 😤"


async def update_last_activity(guild_id: int) -> None:
    """Actualiza el registro de última actividad para un servidor"""
    last_activity[guild_id] = time.time()
    logger.debug(f"Actividad actualizada para guild {guild_id} - {last_activity[guild_id]}")

async def get_voice_notice_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Busca el mejor canal de texto para avisar por qué el bot se desconectó."""
    candidate_ids = [
        last_text_channel_ids.get(guild.id),
        TICKET_LOG_CHANNEL_ID if 'TICKET_LOG_CHANNEL_ID' in globals() else None,
        WELCOME_CHANNEL_ID if 'WELCOME_CHANNEL_ID' in globals() else None,
        getattr(guild.system_channel, 'id', None),
    ]

    seen = set()
    for channel_id in candidate_ids:
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            me = guild.me or guild.get_member(bot.user.id)
            if me and channel.permissions_for(me).send_messages:
                return channel

    for channel in guild.text_channels:
        me = guild.me or guild.get_member(bot.user.id)
        if me and channel.permissions_for(me).send_messages:
            return channel

    return None


async def disconnect_voice_with_notice(
    guild: discord.Guild,
    voice_client: discord.VoiceClient,
    *,
    reason: str,
    idle_seconds: float,
    humans_count: int,
) -> None:
    """Desconecta de voz y avisa en texto sin romper el bot si faltan permisos."""
    guild_id = guild.id
    voice_channel_name = getattr(getattr(voice_client, 'channel', None), 'name', 'canal de voz')
    minutes = max(1, int(idle_seconds // 60))

    embed = discord.Embed(
        title="🔌 Me desconecté de voz",
        description=(
            f"Salí de **{voice_channel_name}** porque **{reason}**.\n\n"
            f"⏱️ Tiempo sin actividad: **{minutes} min**\n"
            f"👥 Usuarios humanos en llamada: **{humans_count}**\n\n"
            "Cuando quieran música otra vez, usen `¡play`, `¡dj` o `/play`."
        ),
        color=discord.Color.orange()
    )
    embed.set_footer(text="Archeon • Limpieza automática de voz")

    channel = await get_voice_notice_channel(guild)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception:
            logger.warning(f"No pude enviar aviso de autodesconexión en {guild.name}: {traceback.format_exc()}")

    try:
        await voice_client.disconnect(force=True)
    except TypeError:
        await voice_client.disconnect()

    # Limpieza segura de estados que ya no aplican.
    current_songs.pop(guild_id, None)
    queues.pop(guild_id, None)
    last_activity.pop(guild_id, None)
    voice_stay_connected_guilds.discard(guild_id)

    if guild_id in dj_sessions:
        dj_sessions[guild_id]["active"] = False
    task = dj_auto_tasks.pop(guild_id, None) if 'dj_auto_tasks' in globals() else None
    if task and not task.done():
        task.cancel()

    logger.info(f"Auto-desconectado de voz en {guild.name}: {reason}")


async def check_empty_voice_channels():
    """
    Vigila voz y desconecta cuando corresponde.

    Reglas:
    - Si hay música, cola, DJ o karaoke activo, NO se desconecta.
    - Si está solo en la llamada, se desconecta tras VOICE_ALONE_TIMEOUT.
    - Si hay gente pero nadie usa el bot, se desconecta tras VOICE_IDLE_TIMEOUT.
    - Si activaste `¡mantener_voz on`, no se baja solo.
    """
    while True:
        await asyncio.sleep(VOICE_CHECK_INTERVAL)
        current_time = time.time()

        for guild in list(bot.guilds):
            try:
                voice_client = guild.voice_client
                if not voice_client or not voice_client.is_connected():
                    continue

                guild_id = guild.id
                channel = voice_client.channel
                if channel:
                    last_voice_channel_ids[guild_id] = channel.id

                humans = [m for m in getattr(channel, "members", []) if not getattr(m, "bot", False)]
                humans_count = len(humans)

                karaoke_sessions = globals().get("KARAOKE_SESSIONS", {})
                karaoke_active = guild_id in karaoke_sessions and karaoke_sessions[guild_id].get("active", False)
                dj_active = bool(dj_sessions.get(guild_id, {}).get("active", False))
                music_active = (
                    voice_client.is_playing()
                    or voice_client.is_paused()
                    or bool(queues.get(guild_id))
                    or dj_active
                    or karaoke_active
                )

                # Si está haciendo algo real, se queda. Aquí sí actualizamos actividad.
                if music_active or guild_id in voice_stay_connected_guilds:
                    last_activity[guild_id] = current_time
                    continue

                # Si el usuario decidió apagar la autodesconexión, no tocamos nada.
                if not AUTO_VOICE_DISCONNECT_ENABLED:
                    continue

                # Primera vez que lo vemos quieto: arrancamos contador, no lo sacamos de golpe.
                idle_since = last_activity.setdefault(guild_id, current_time)
                idle_seconds = current_time - idle_since

                if humans_count == 0:
                    timeout = max(30, VOICE_ALONE_TIMEOUT)
                    reason = "me quedé solo en la llamada"
                else:
                    timeout = max(60, VOICE_IDLE_TIMEOUT)
                    reason = "nadie usó comandos de música ni DJ por un rato"

                if idle_seconds >= timeout:
                    await disconnect_voice_with_notice(
                        guild,
                        voice_client,
                        reason=reason,
                        idle_seconds=idle_seconds,
                        humans_count=humans_count,
                    )

            except Exception:
                logger.error(f"Error en check_empty_voice_channels para {getattr(guild, 'name', 'guild desconocido')}: {traceback.format_exc()}")
                await asyncio.sleep(5)


async def extract_info_async(ydl, query, download=False):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(ydl.extract_info, query, download=download))


def _safe_int(value, default: int = 0) -> int:
    """Convierte valores raros de yt-dlp a int sin romper el bot."""
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


class MusicSearchError(Exception):
    """Error controlado cuando no se encuentra una pista reproducible."""
    pass


def _clean_search_query(query: str) -> str:
    """Limpia el texto para búsquedas musicales sin pasarse de listo."""
    query = (query or "").strip()
    query = re.sub(r"\s+", " ", query)
    return query[:180]


def _is_gemini_quota_error(error: BaseException) -> bool:
    """Detecta cuando Gemini se queda sin cuota para no matar el DJ."""
    text_error = str(error).lower()
    return (
        "429" in text_error
        or "quota" in text_error
        or "rate limit" in text_error
        or "resource_exhausted" in text_error
        or "exceeded your current quota" in text_error
    )


async def safe_gemini_text(prompt: str, *, context: str = "DJ") -> Optional[str]:
    """
    Usa Gemini solo si está permitido y disponible.
    Si falla por cuota 429, activa cooldown y devuelve None para usar fallback local.
    """
    global gemini_dj_cooldown_until

    if not DJ_USE_GEMINI or model is None:
        return None

    now = time.time()
    if now < gemini_dj_cooldown_until:
        return None

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, partial(model.generate_content, prompt))
        return (getattr(response, "text", "") or "").strip() or None
    except Exception as e:
        if _is_gemini_quota_error(e):
            # Evita spamear Gemini durante una hora después de un 429.
            gemini_dj_cooldown_until = time.time() + 3600
            logger.warning(f"Gemini sin cuota en {context}. DJ seguirá con fallback local: {str(e)[:180]}")
        else:
            logger.warning(f"Gemini falló en {context}. DJ seguirá con fallback local: {str(e)[:180]}")
        return None


def _strip_music_noise(text: str) -> str:
    """Limpia etiquetas típicas de YouTube para comparar y buscar mejor."""
    text = str(text or "")
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)
    text = re.sub(
        r"\b(official|audio|video|lyrics?|lyric|letra|visualizer|hd|4k|remaster(?:ed)?|oficial)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip()


def _extract_artist_seed(query: str) -> str:
    """Si el usuario pone 'Artista - Canción', usa el artista como semilla del DJ."""
    q = _strip_music_noise(query)
    for sep in (" - ", " – ", " — ", " | "):
        if sep in q:
            artist = q.split(sep, 1)[0].strip()
            if len(artist) >= 3:
                return artist
    return q


def build_dj_fallback_terms(query: str, *, limit: int = 7) -> List[str]:
    """
    Términos buenos sin IA para que el DJ tenga variedad.

    Antes metía la canción exacta + official audio, y YouTube devolvía el mismo tema una y otra vez.
    Ahora, si detecta 'Artista - Canción', prioriza radio/mix/canciones del artista.
    """
    q = _clean_search_query(query)
    if not q:
        return []

    seed = _clean_search_query(_extract_artist_seed(q))
    candidates: List[str] = []

    if seed and seed.lower() != q.lower():
        candidates.extend([
            f"{seed} canciones populares",
            f"{seed} mejores canciones",
            f"{seed} mix",
            f"{seed} radio",
            f"{seed} playlist",
            f"música similar a {seed}",
            f"artistas similares a {seed}",
            f"{q} radio mix",
            f"{q} canciones similares",
        ])
    else:
        candidates.extend([
            f"{q} radio mix",
            f"{q} canciones similares",
            f"{q} mix",
            f"{q} playlist",
            f"música similar a {q}",
            f"{q} mejores canciones",
            f"{q} populares",
        ])

    unique = []
    seen = set()
    for term in candidates:
        clean = _clean_search_query(term)
        key = clean.lower()
        if clean and key not in seen:
            unique.append(clean)
            seen.add(key)
    return unique[:limit]


def _normalize_song_title(text: str) -> str:
    text = _strip_music_noise(text).lower()
    text = re.sub(r"[^a-z0-9áéíóúüñ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _canonical_web_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower().replace("www.", "")
        query = urllib.parse.parse_qs(parsed.query)
        if "youtube.com" in host and query.get("v"):
            return f"youtube:{query['v'][0]}"
        if "youtu.be" in host:
            return f"youtube:{parsed.path.strip('/')}"
        if "soundcloud.com" in host:
            return f"soundcloud:{parsed.path.strip('/').lower()}"
        return f"{host}{parsed.path}".lower().rstrip("/")
    except Exception:
        return url.lower()


def song_markers(song: Dict) -> Set[str]:
    """Marcadores para detectar la misma canción aunque cambie la URL temporal de audio."""
    markers: Set[str] = set()
    web = _canonical_web_url(song.get("web_url") or song.get("original_url") or "")
    if web:
        markers.add("web:" + web)
    title = _normalize_song_title(song.get("title") or "")
    uploader = _normalize_song_title(song.get("uploader") or song.get("channel") or "")
    if title:
        markers.add("title:" + title)
    if title and uploader:
        markers.add("title_uploader:" + title + "|" + uploader)
    return markers


def remember_dj_song(guild_id: int, song: Dict) -> None:
    markers = list(song_markers(song))
    if not markers:
        return
    bucket = dj_recent_markers.setdefault(guild_id, [])
    bucket.extend(markers)
    # Mantiene memoria razonable sin crecer infinito.
    max_items = max(10, DJ_RECENT_MEMORY * 3)
    if len(bucket) > max_items:
        del bucket[:-max_items]


def get_dj_exclusion_markers(guild_id: int) -> Set[str]:
    markers: Set[str] = set(dj_recent_markers.get(guild_id, []))
    for song in queues.get(guild_id, []):
        markers.update(song_markers(song))
    current = current_songs.get(guild_id)
    if current:
        markers.update(song_markers(current))
    return markers


def is_song_blocked_for_dj(guild_id: int, song: Dict, *, extra_markers: Optional[Set[str]] = None) -> bool:
    markers = song_markers(song)
    if not markers:
        return False
    blocked = get_dj_exclusion_markers(guild_id)
    if extra_markers:
        blocked.update(extra_markers)
    return bool(markers & blocked)


def ensure_dj_auto_task(guild: discord.Guild) -> None:
    """Evita crear varias tareas DJ para el mismo servidor."""
    task = dj_auto_tasks.get(guild.id)
    if task and not task.done():
        return
    dj_auto_tasks[guild.id] = create_logged_task(auto_add_songs_task(guild), f"dj_auto_add_{guild.id}")


def _is_youtube_bot_check_error(error: BaseException) -> bool:
    text_error = str(error).lower()
    return (
        "sign in to confirm" in text_error
        or "not a bot" in text_error
        or "confirm you" in text_error
        or "cookies" in text_error and "youtube" in text_error
    )


def _query_wants_version(query: str, word: str) -> bool:
    """Devuelve True si el usuario pidió explícitamente una versión tipo remix/live/etc."""
    return word.lower() in str(query or "").lower()


def _music_tokens(text: str) -> Set[str]:
    """Tokens limpios para comparar búsqueda vs resultado."""
    cleaned = _strip_music_noise(text).lower()
    return {
        t for t in re.findall(r"[a-z0-9áéíóúüñ]+", cleaned)
        if len(t) > 1 and t not in {"oficial", "official", "audio", "video", "lyrics", "letra", "the", "and", "feat", "ft"}
    }


def _music_match_ratio(info: Dict, query: str) -> float:
    """Qué tanto coincide el resultado con lo pedido. 1.0 = casi exacto."""
    q_tokens = _music_tokens(query)
    if not q_tokens:
        return 0.0
    title = str(info.get("title") or "")
    uploader = str(info.get("uploader") or info.get("channel") or "")
    result_tokens = _music_tokens(f"{title} {uploader}")
    if not result_tokens:
        return 0.0
    return len(q_tokens & result_tokens) / max(1, len(q_tokens))


def _is_too_weak_music_match(info: Dict, query: str) -> bool:
    """Evita que Render/SoundCloud pongan 'algo parecido' cuando el usuario pidió una canción concreta."""
    if not STRICT_MUSIC_MATCH:
        return False
    if URL_REGEX.match(str(query or "")):
        return False
    ratio = _music_match_ratio(info, query)
    # Para búsquedas cortas como "julieta latin mafia" exigimos casi todo.
    # Así no pone remixes raros ni canciones con solo una palabra en común.
    return ratio < 0.66


def _is_unwanted_music_version(info: Dict, query: str) -> bool:
    """
    Evita reproducir remixes/covers/live/slowed si el usuario no los pidió.
    Mejor fallar con mensaje claro que poner una canción equivocada.
    """
    title = str(info.get("title") or "").lower()
    uploader = str(info.get("uploader") or info.get("channel") or "").lower()
    haystack = f"{title} {uploader}"
    q = str(query or "").lower()

    unwanted_words = [
        "remix", "cover", "karaoke", "reaction", "tutorial",
        "slowed", "reverb", "speed up", "sped up", "nightcore",
        "instrumental", "live", "en vivo", "8d", "bass boosted",
        "edit", "tiktok", "tik tok", "mashup", "version salsa",
        "versión salsa", "acoustic", "acústico"
    ]

    for word in unwanted_words:
        if word in haystack and word not in q:
            return True
    return False


def _score_music_candidate(info: Dict, query: str) -> int:
    """Puntúa resultados para elegir mejor la canción exacta y evitar remixes falsos."""
    title_raw = str(info.get("title") or "")
    uploader_raw = str(info.get("uploader") or info.get("channel") or "")
    title = title_raw.lower()
    uploader = uploader_raw.lower()
    q = str(query or "").lower()
    haystack = f"{title} {uploader}"

    tokens = [
        t for t in re.findall(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ0-9]+", q)
        if len(t) > 1
    ]

    score = 0
    matched = 0

    for token in tokens:
        if token in title:
            score += 12
            matched += 1
        elif token in uploader:
            score += 8
            matched += 1
        elif token in haystack:
            score += 3

    # Si coincide casi todo lo que pidió el usuario, probablemente es la canción correcta.
    if tokens and matched >= max(1, len(tokens) - 1):
        score += 35

    clean_q = re.sub(r"\s+", " ", q).strip()
    clean_title = re.sub(r"\s+", " ", title).strip()
    if clean_q and clean_q in clean_title:
        score += 45

    duration = _safe_int(info.get("duration"), 0)
    if 90 <= duration <= 420:
        score += 10
    elif duration and duration > 600:
        score -= 25

    # Penalizar versiones no pedidas. Si el usuario pidió "remix", entonces remix sí compite.
    bad_words = [
        "remix", "cover", "karaoke", "reaction", "tutorial",
        "slowed", "reverb", "speed up", "sped up", "nightcore",
        "instrumental", "live", "en vivo", "8d", "bass boosted",
        "edit", "tiktok", "tik tok", "mashup", "version salsa",
        "versión salsa", "acoustic", "acústico"
    ]
    for bad in bad_words:
        if bad in title and bad not in q:
            score -= 80

    good_words = [
        "official audio", "audio oficial", "video oficial",
        "official video", "letra", "lyrics"
    ]
    for good in good_words:
        if good in title:
            score += 10

    # Si el canal/uploader contiene artista buscado, es señal fuerte.
    for token in tokens:
        if token in uploader:
            score += 10

    return score


def _iter_ydl_entries(info) -> List[Dict]:
    """Devuelve entradas válidas sin reventar cuando yt-dlp mete None en entries."""
    if not info:
        return []

    if isinstance(info, dict) and "entries" in info:
        return [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]

    return [info] if isinstance(info, dict) else []


def normalize_ydl_info(info, query: str) -> Dict:
    """
    Normaliza una respuesta de yt-dlp y elige la mejor entrada reproducible.
    Antes tomaba entries[0]; si ese resultado estaba bloqueado, el bot decía que no existía la canción.
    """
    entries = _iter_ydl_entries(info)
    if not entries:
        raise MusicSearchError(f"No encontré resultados reproducibles para: {query}")

    # Primero probamos resultados con audio real.
    playable = []
    for entry in entries:
        try:
            get_best_audio_url(entry)
            playable.append(entry)
        except Exception:
            continue

    if not playable:
        raise MusicSearchError(f"Encontré resultados para `{query}`, pero ninguno traía audio reproducible.")

    return max(playable, key=lambda item: _score_music_candidate(item, query))


def get_best_audio_url(info: Dict) -> str:
    """Obtiene una URL de audio segura desde la respuesta de yt-dlp."""
    formats = info.get("formats") or []

    # Preferimos formatos solo-audio.
    best_format = next(
        (
            f for f in formats
            if isinstance(f, dict)
            and f.get("url")
            and f.get("acodec") not in (None, "none")
            and f.get("vcodec") in (None, "none")
        ),
        None
    )

    # Si no hay solo-audio, usamos cualquier formato con audio.
    if best_format is None:
        best_format = next(
            (
                f for f in formats
                if isinstance(f, dict)
                and f.get("url")
                and f.get("acodec") not in (None, "none")
            ),
            None
        )

    if best_format and best_format.get("url"):
        return best_format["url"]

    # Algunos extractores entregan la URL directa en info["url"].
    if info.get("url") and (info.get("acodec") not in (None, "none") or not formats):
        return info["url"]

    raise MusicSearchError("El resultado encontrado no trae audio reproducible.")




def get_guild_volume(guild_id: int) -> float:
    """Volumen por servidor entre 0.0 y 2.0."""
    return max(0.0, min(2.0, guild_volumes.get(guild_id, DEFAULT_GUILD_VOLUME)))


async def create_audio_source(url: str, guild_id: Optional[int] = None):
    """Crea fuente de audio usando primero el método del bot 18.

    En Render, FFmpegOpusAudio.from_probe fue el comportamiento que ya te funcionaba.
    Si por alguna razón falla, recién intenta el método nuevo con PCMVolumeTransformer.
    """
    try:
        try:
            return await discord.FFmpegOpusAudio.from_probe(
                url,
                method='fallback',
                **LEGACY_FFMPEG_OPTIONS
            )
        except Exception as legacy_error:
            logger.warning("FFmpeg legacy falló, intento audio PCM: %s", str(legacy_error)[:180])

        raw_source = discord.FFmpegPCMAudio(
            url,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_AUDIO_OPTIONS,
            executable=FFMPEG_EXECUTABLE
        )
        volume = get_guild_volume(guild_id or 0)
        return discord.PCMVolumeTransformer(raw_source, volume=volume)
    except FileNotFoundError as e:
        raise RuntimeError(
            "ffmpeg no está instalado o no está en el PATH. En Render necesitas que FFmpeg exista en el entorno."
        ) from e


def build_music_searches(busqueda: str) -> List[str]:
    """Arma búsquedas precisas y respaldos sin poner remixes por accidente.

    En Render YouTube puede bloquear formatos. Por eso hacemos:
    1) YouTube exacto.
    2) YouTube oficial.
    3) SoundCloud exacto como respaldo.
    4) Variantes de letra/video solo si hace falta.
    """
    query = _clean_search_query(busqueda)
    if not query:
        return []

    if URL_REGEX.match(query):
        return [query]

    q_lower = query.lower()
    wants_alt_version = any(
        word in q_lower
        for word in ("remix", "cover", "live", "en vivo", "slowed", "karaoke", "instrumental", "sped up", "reverb")
    )

    # YouTube sí interpreta algunos negativos en búsqueda. No es perfecto, pero ayuda.
    negative = ""
    if not wants_alt_version:
        negative = " -remix -cover -slowed -reverb -karaoke -live -instrumental -nightcore -sped -tiktok"

    searches = [
        f"ytsearch15:{query}{negative}",
        f"ytsearch15:{query} official audio{negative}",
        f"ytsearch15:{query} audio oficial{negative}",
        f"ytsearch15:{query} official video{negative}",
        f"ytsearch15:{query} video oficial{negative}",
        # Respaldo exacto antes de búsquedas más flojas.
        f"scsearch10:{query}",
        f"ytsearch10:{query} lyrics{negative}",
        f"ytsearch10:{query} letra{negative}",
    ]

    unique = []
    seen = set()
    for item in searches:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique

def _format_music_error(query: str, bot_blocked: bool = False) -> str:
    if bot_blocked:
        return (
            f"YouTube sí encontró algo para `{query}`, pero bloqueó la extracción con verificación anti-bot. "
            "Probé resultados alternativos y respaldo, pero no salió una URL reproducible. "
            "Prueba pegando el enlace exacto o usa `cookies.txt` exportado del navegador."
        )

    return (
        f"No pude obtener audio reproducible para `{query}`. "
        "Render/YouTube no entregó un stream de audio válido. Prueba pegando el enlace directo, "
        "añade artista + canción exacta o intenta una fuente de SoundCloud."
    )


async def resolve_song(
    busqueda: str,
    requested_by,
    *,
    max_duration: Optional[int] = None,
    exclude_markers: Optional[Set[str]] = None,
) -> Dict:
    """Busca una canción con el método viejo que sí funcionaba en Render.

    Esto restaura el flujo del bot 18:
    ytsearch:{busqueda} -> primera entrada -> info['url'] o primer formato con audio.
    Nada de estrategias raras que saltaban resultados válidos.
    """
    query = _clean_search_query(busqueda)
    if not query:
        raise MusicSearchError("Escribe el nombre o enlace de una canción.")

    exclude_markers = set(exclude_markers or set())

    try:
        info, audio_url = await extract_music_legacy_like_bot18(query)

        duration = _safe_int(info.get("duration"), 0)
        if max_duration and duration and duration > max_duration:
            raise MusicSearchError("La canción supera la duración máxima permitida.")

        song = {
            "title": str(info.get("title") or query)[:100],
            "url": audio_url,
            "web_url": info.get("webpage_url") or info.get("original_url") or query,
            "duration": duration,
            "requested_by": requested_by,
            "thumbnail": info.get("thumbnail") or "https://i.imgur.com/8Km9tLL.png",
            "uploader": str(info.get("uploader") or info.get("channel") or "Artista desconocido")[:80],
        }

        if exclude_markers and (song_markers(song) & exclude_markers):
            raise MusicSearchError("La canción ya está sonando, en cola o fue usada recientemente.")

        logger.info("Canción resuelta con extractor estilo bot 18: %s", song["title"])
        return song

    except Exception as e:
        logger.warning("Extractor estilo bot 18 falló para '%s': %s", query, str(e)[:220])
        raise MusicSearchError(
            f"No pude obtener audio reproducible para `{query}`. "
            "Esto ya apunta a bloqueo de YouTube en Render o cookies inválidas. "
            "Prueba enlace directo de YouTube o actualiza el Secret File cookies.txt."
        ) from e


async def add_song_to_queue(guild_id: int, song: Dict, *, front: bool = False, avoid_recent: bool = False) -> bool:
    """Añade una canción evitando duplicados por URL, título, canción actual y memoria DJ."""
    queue_ref = queues.setdefault(guild_id, [])
    song_url = song.get("url")
    song_title = _normalize_song_title(song.get("title") or "")
    markers = song_markers(song)

    current = current_songs.get(guild_id)
    if current and markers and (markers & song_markers(current)):
        return False

    for existing in queue_ref:
        if existing.get("url") == song_url:
            return False
        existing_title = _normalize_song_title(existing.get("title") or "")
        if song_title and existing_title and song_title == existing_title:
            return False
        if markers and (markers & song_markers(existing)):
            return False

    if avoid_recent and markers & set(dj_recent_markers.get(guild_id, [])):
        return False

    if front:
        queue_ref.insert(0, song)
    else:
        queue_ref.append(song)
    return True


def get_voice_client_from_context(ctx: Union[commands.Context, discord.Interaction, discord.Guild]):
    """Devuelve el voice_client para Context, Interaction o Guild."""
    if isinstance(ctx, discord.Interaction):
        return ctx.guild.voice_client if ctx.guild else None
    if isinstance(ctx, discord.Guild):
        return ctx.voice_client
    return getattr(ctx, 'voice_client', None)


async def send_context_message(ctx: Union[commands.Context, discord.Interaction, discord.Guild], *args, **kwargs):
    """Envía mensajes sin romperse entre Context, Interaction o Guild."""
    if isinstance(ctx, discord.Interaction):
        if ctx.response.is_done():
            return await ctx.followup.send(*args, **kwargs)
        return await ctx.response.send_message(*args, **kwargs)

    if hasattr(ctx, 'send'):
        return await ctx.send(*args, **kwargs)

    guild = ctx if isinstance(ctx, discord.Guild) else getattr(ctx, 'guild', None)
    if guild:
        channel = guild.system_channel or next(
            (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
            None
        )
        if channel:
            return await channel.send(*args, **kwargs)

    logger.warning('No se pudo enviar mensaje: no hay canal disponible')
    return None


async def safe_disconnect_existing_voice(guild: discord.Guild) -> None:
    """Limpia una conexión de voz fantasma antes de volver a conectar."""
    voice_client = guild.voice_client
    if not voice_client:
        return

    try:
        await voice_client.disconnect(force=True)
    except TypeError:
        try:
            await voice_client.disconnect()
        except Exception:
            pass
    except Exception:
        pass

    await asyncio.sleep(1)


async def safe_connect(channel, max_retries=2, initial_delay=2.0):
    """
    Conexión segura a voz.

    Importante: si aparece 4017, NO es un problema de permisos ni del canal.
    Es falta de soporte DAVE/E2EE en las dependencias de voz.
    """
    guild = channel.guild
    guild_id = guild.id
    last_error = None

    existing = guild.voice_client
    if existing:
        try:
            if existing.is_connected():
                if getattr(existing, "channel", None) == channel:
                    return existing
                await existing.move_to(channel)
                last_activity[guild_id] = time.time()
                return existing
            await safe_disconnect_existing_voice(guild)
        except Exception:
            await safe_disconnect_existing_voice(guild)

    for attempt in range(1, max_retries + 1):
        voice_connection_attempts[guild_id] = time.time()
        try:
            logger.info(f"Intentando conectar a voz: {guild.name} / {channel.name} (intento {attempt}/{max_retries})")

            # reconnect=False evita el spam interno de reintentos cuando Discord ya rechazó por 4017.
            try:
                voice_client = await channel.connect(
                    timeout=25.0,
                    reconnect=False,
                    self_deaf=False
                )
            except TypeError:
                # Compatibilidad con versiones antiguas de discord.py.
                # Si tu versión es tan vieja que no acepta self_deaf, igual conviene actualizar por DAVE.
                voice_client = await channel.connect(
                    timeout=25.0,
                    reconnect=False
                )

            last_activity[guild_id] = time.time()
            last_voice_channel_ids[guild_id] = channel.id
            logger.info(f"Conexión exitosa al canal de voz {channel.name}")
            return voice_client

        except Exception as e:
            last_error = e
            logger.error(f"Error conectando a voz (intento {attempt}/{max_retries}): {repr(e)}")

            if is_voice_4017_error(e):
                logger.error(VOICE_4017_HINT)
                raise RuntimeError(VOICE_4017_HINT) from e

            await safe_disconnect_existing_voice(guild)

            if attempt < max_retries:
                await asyncio.sleep(initial_delay * attempt)

    if last_error:
        raise last_error

    raise RuntimeError("No se pudo conectar al canal de voz por una causa desconocida.")



async def recover_music_after_disconnect(guild: discord.Guild, voice_channel: Optional[discord.VoiceChannel] = None):
    """Reconecta y recupera música si Discord o la red sacan al bot del canal."""
    guild_id = guild.id
    try:
        if guild.voice_client and guild.voice_client.is_connected():
            return

        if voice_channel is None:
            channel_id = last_voice_channel_ids.get(guild_id)
            voice_channel = guild.get_channel(channel_id) if channel_id else None

        if not voice_channel:
            logger.warning(f"No pude recuperar voz en guild {guild_id}: no tengo canal guardado.")
            return

        await asyncio.sleep(3)
        voice_client = await safe_connect(voice_channel)

        current_song = current_songs.get(guild_id)
        if current_song and not voice_client.is_playing() and not voice_client.is_paused():
            logger.info(f"Recuperando reproducción en {guild.name}: {current_song.get('title')}")
            source = await create_audio_source(current_song["url"], guild_id)
            voice_client.play(
                source,
                after=lambda e: asyncio.run_coroutine_threadsafe(
                    check_queue(guild),
                    bot.loop
                ) if e is None else logger.error(f"Error en reproducción recuperada: {e}")
            )

            embed = discord.Embed(
                title="🔁 Voz recuperada",
                description=f"Me reconecté y retomé: [{current_song.get('title', 'canción')}]({current_song.get('web_url', '')})",
                color=discord.Color.green()
            )
            await send_context_message(guild, embed=embed)

        elif queues.get(guild_id):
            await check_queue(guild)

    except Exception:
        logger.error(f"No pude recuperar música tras desconexión: {traceback.format_exc()}")


async def music_voice_watchdog():
    """Red de seguridad: si hay música/cola y el bot quedó fuera de voz, intenta volver."""
    while True:
        await asyncio.sleep(45)
        for guild in list(bot.guilds):
            try:
                guild_id = guild.id
                active_state = bool(current_songs.get(guild_id) or queues.get(guild_id) or dj_sessions.get(guild_id, {}).get("active", False))
                karaoke_sessions = globals().get("KARAOKE_SESSIONS", {})
                active_state = active_state or bool(guild_id in karaoke_sessions and karaoke_sessions[guild_id].get("active", False))

                if not active_state:
                    continue

                voice_client = guild.voice_client
                if voice_client and voice_client.is_connected():
                    continue

                channel_id = last_voice_channel_ids.get(guild_id)
                channel = guild.get_channel(channel_id) if channel_id else None
                if channel:
                    logger.warning(f"Watchdog: detecté música activa sin voz en {guild.name}; intento reconectar.")
                    create_logged_task(recover_music_after_disconnect(guild, channel), f"recover_music_{guild.id}")

            except Exception:
                logger.error(f"Error en music_voice_watchdog: {traceback.format_exc()}")


@bot.command(name="mantener_voz", aliases=["stay", "247", "24_7", "no_salir"])
async def mantener_voz(ctx: commands.Context, modo: str = "on"):
    """Activa/desactiva que el bot se quede conectado aunque no haya música."""
    modo = (modo or "on").lower().strip()
    if modo in {"on", "si", "sí", "true", "1", "activar"}:
        voice_stay_connected_guilds.add(ctx.guild.id)
        await ctx.send("✅ Modo **mantener voz** activado. No me bajo solo de la llamada.")
    elif modo in {"off", "no", "false", "0", "desactivar"}:
        voice_stay_connected_guilds.discard(ctx.guild.id)
        await ctx.send("✅ Modo **mantener voz** desactivado.")
    else:
        estado = "activado" if ctx.guild.id in voice_stay_connected_guilds else "desactivado"
        await ctx.send(f"ℹ️ Uso: `¡mantener_voz on/off`. Estado actual: **{estado}**.")


@bot.command(name="voz_estado", aliases=["estado_voz", "debugvoz"])
async def voz_estado(ctx: commands.Context):
    """Muestra diagnóstico rápido de voz/música."""
    vc = ctx.guild.voice_client
    embed = discord.Embed(title="🔎 Estado de voz", color=discord.Color.blurple())
    embed.add_field(name="Conectado", value="Sí" if vc and vc.is_connected() else "No", inline=True)
    embed.add_field(name="Reproduciendo", value="Sí" if vc and vc.is_playing() else "No", inline=True)
    embed.add_field(name="Pausado", value="Sí" if vc and vc.is_paused() else "No", inline=True)
    embed.add_field(name="Canal guardado", value=str(last_voice_channel_ids.get(ctx.guild.id, "Ninguno")), inline=False)
    embed.add_field(name="Actual", value=(current_songs.get(ctx.guild.id, {}) or {}).get("title", "Nada"), inline=False)
    embed.add_field(name="Cola", value=str(len(queues.get(ctx.guild.id, []))), inline=True)
    embed.add_field(name="Mantener voz", value="Sí" if ctx.guild.id in voice_stay_connected_guilds else "No", inline=True)
    await ctx.send(embed=embed)


@bot.command(name='unirse', aliases=['join', 'entrar'], help='Hace que el bot se una al canal de voz')
async def join(ctx: commands.Context) -> None:
    """Une al bot al canal de voz con diagnóstico claro para errores 4017."""
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        return await ctx.send("❌ ¡No estás en un canal de voz!")

    channel = ctx.author.voice.channel

    try:
        voice_client = await safe_connect(channel)

        # Ajustes seguros si la implementación los acepta.
        try:
            voice_client.encoder_options = {
                'channels': 2,
                'frame_length': 60,
                'sample_rate': 48000,
                'bitrate': '128k'
            }
        except Exception:
            pass

        await ctx.send(f"🔊 Conectado a **{channel.name}**")

    except Exception as e:
        logger.error(f"Error en join/unirse: {traceback.format_exc()}")
        await ctx.send(human_voice_error(e))


@bot.command(name='play' , aliases=['p', 'reproduce', 'ponme', 'reproducir', 'PLAY'])
async def play(ctx: commands.Context, *, busqueda: str) -> None:
    """Reproduce música o la añade a la cola"""
    if not check_same_voice_channel(ctx):
        return await ctx.send("❌ Debes estar en el mismo canal de voz que el bot para usar este comando.")
    await update_last_activity(ctx.guild.id)

    if not ctx.author.voice:
        return await ctx.send("❌ ¡No estás en un canal de voz!")

    last_voice_channel_ids[ctx.guild.id] = ctx.author.voice.channel.id

    embed_cargando = discord.Embed(
        title="🎵🔍 Buscando tu canción...",
        description="⌛ Procesando tu solicitud, por favor espera...\n\n🌟 *Pronto disfrutarás de tu música favorita*",
        color=discord.Color.blurple()
    )
    embed_cargando.set_thumbnail(url="https://media4.giphy.com/media/v1.Y2lkPTc5MGI3NjExNXM2cHp2MDR2anZxeHU2d2c0dDM4a2RyYm1iNmEyaHhvY3J2bGM1dCZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/f31DK1KpGsyMU/giphy.gif")
    embed_cargando.set_footer(text="🎶 Paciencia, buena música está por venir...")

    cargando_msg = await ctx.send(embed=embed_cargando)

    try:
        voice_client = ctx.voice_client or await safe_connect(ctx.author.voice.channel)
        song = await resolve_song(busqueda, ctx.author)

        await cargando_msg.delete()

        if voice_client.is_playing() or voice_client.is_paused():
            await add_song_to_queue(ctx.guild.id, song)

            embed = discord.Embed(
                title="🎵 Añadido a la cola",
                description=f"[{song['title']}]({song['web_url']})",
                color=discord.Color.green()
            )
            embed.add_field(name="Posición en cola", value=str(len(queues.get(ctx.guild.id, []))))
            if song.get('thumbnail'):
                embed.set_thumbnail(url=song['thumbnail'])
            embed.set_footer(text=f"Solicitado por {ctx.author.display_name}")
            return await ctx.send(embed=embed)

        current_songs[ctx.guild.id] = song
        save_to_history(ctx.guild.id, song)

        source = await create_audio_source(song["url"], ctx.guild.id)

        voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(
                check_queue(ctx),
                bot.loop
            ) if e is None else logger.error(f'Error en reproducción: {e}')
        )

        embed = discord.Embed(
            title="🎵 Reproduciendo ahora",
            description=f"[{song['title']}]({song['web_url']})",
            color=discord.Color.blurple()
        )
        duration = song.get('duration', 0)
        embed.add_field(
            name="Duración",
            value=f"{duration // 60}:{duration % 60:02d}" if duration else "Desconocida"
        )
        if song.get('thumbnail'):
            embed.set_thumbnail(url=song['thumbnail'])
        embed.set_footer(text=f"Solicitado por {ctx.author.display_name}")

        await ctx.send(embed=embed)

    except Exception as e:
        try:
            await cargando_msg.delete()
        except Exception:
            pass

        if is_voice_4017_error(e) or "4017" in str(e):
            await ctx.send(human_voice_error(e))
        else:
            error_msg = str(e)
            if "NoneType" in error_msg:
                error_msg = "El extractor devolvió un resultado vacío. Prueba con el nombre exacto o pega el enlace."
            await ctx.send(f"❌ Error al reproducir: {error_msg}"[:2000])
        logger.error(f"Error en play: {traceback.format_exc()}")


@bot.command(name='saltar', aliases=['skip', 's'])
async def skip(ctx: commands.Context) -> None:
    """Salta la canción actual y pasa a la siguiente en la cola"""
    voice = ctx.voice_client

    if not voice or not voice.is_playing():
        await ctx.send("⚠️ No hay música reproduciéndose.")
        return

    await update_last_activity(ctx.guild.id)
    voice.stop()
    await ctx.send("⏭️ Canción saltada")


@bot.command(name='pausar', aliases=['pause'])
async def pause(ctx: commands.Context) -> None:
    """Pausa la música"""
    if not check_same_voice_channel(ctx):
        return await ctx.send("❌ Debes estar en el mismo canal de voz que el bot para usar este comando.")

    voice = ctx.voice_client
    if voice and voice.is_playing():
        await update_last_activity(ctx.guild.id)
        voice.pause()
        await ctx.send("⏸️ Música pausada")
    else:
        await ctx.send("⚠️ No hay música reproduciéndose")


@bot.command(name='continuar', aliases=['resume'])
async def resume(ctx: commands.Context) -> None:
    """Reanuda la música"""
    voice = ctx.voice_client
    if voice and voice.is_paused():
        await update_last_activity(ctx.guild.id)
        voice.resume()
        await ctx.send("▶️ Música reanudada")
    else:
        await ctx.send("⚠️ La música no está pausada")


@bot.command(name='cola', aliases=['lista', 'queue', 'q'])
async def queue(ctx: commands.Context) -> None:
    """Muestra la cola de reproducción"""
    guild_id = ctx.guild.id
    current_song = current_songs.get(guild_id)
    if not queues.get(guild_id) and not current_song:
        await ctx.send("📭 La cola está vacía")
    else:
        embed = discord.Embed(title="🎶 Cola de reproducción", color=discord.Color.purple())

        if current_song:
            duration = ""
            if current_song.get('duration', 0) > 0:
                mins, secs = divmod(current_song['duration'], 60)
                duration = f" [{mins}:{secs:02d}]"

            requested_by = current_song.get('requested_by')
            requester = requested_by.mention if hasattr(requested_by, 'mention') else 'Desconocido'
            embed.add_field(
                name="🔊 Reproduciendo ahora",
                value=f"**{current_song['title']}**{duration}\nSolicitado por: {requester}",
                inline=False
            )

        if queues.get(guild_id):
            for i, item in enumerate(queues[guild_id][:10]):
                duration = ""
                if item.get('duration', 0) > 0:
                    mins, secs = divmod(item['duration'], 60)
                    duration = f" [{mins}:{secs:02d}]"

                requested_by = item.get('requested_by')
                requester = requested_by.display_name if hasattr(requested_by, 'display_name') else 'Desconocido'
                embed.add_field(
                    name=f"{i + 1}. {item['title']}{duration}",
                    value=f"Solicitado por: {requester}",
                    inline=False
                )

            if len(queues[guild_id]) > 10:
                embed.set_footer(text=f"Y {len(queues[guild_id]) - 10} canciones más en la cola...")

        await ctx.send(embed=embed)


@bot.command(name='desconectar', aliases=['disconnect', 'leave', 'salir'])
async def disconnect(ctx: commands.Context) -> None:
    """Desconecta al bot del canal de voz"""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Desconectado del canal de voz")
    else:
        await ctx.send("No estoy conectado a ningún canal de voz")


@bot.command(name='mezclar', aliases=['shuffle'])
async def shuffle_queue(ctx: commands.Context) -> None:
    """Mezcla aleatoriamente la cola de reproducción"""
    if ctx.guild.id not in queues or len(queues[ctx.guild.id]) < 2:
        return await ctx.send("🔀 Necesitas al menos 2 canciones en la cola para mezclar.")

    random.shuffle(queues[ctx.guild.id])
    await ctx.send("🔀 Cola mezclada aleatoriamente.")


@bot.command(name='eliminar', aliases=['borrar', 'remove'])
async def remove_song(ctx: commands.Context, index: int) -> None:
    """Elimina una canción de la cola por su posición"""
    if ctx.guild.id not in queues or index < 1 or index > len(queues[ctx.guild.id]):
        return await ctx.send("❌ Índice inválido o cola vacía.")

    removed = queues[ctx.guild.id].pop(index - 1)
    await ctx.send(f"🗑️ Canción **{removed['title']}** eliminada de la cola.")


@bot.command(name='volumen', aliases=['volume', 'vol'])
async def volume(ctx: commands.Context, vol: Optional[int] = None) -> None:
    """Ajusta el volumen de reproducción por servidor."""
    guild_id = ctx.guild.id

    if vol is None:
        current_vol = int(get_guild_volume(guild_id) * 100)
        if ctx.voice_client and ctx.voice_client.source and hasattr(ctx.voice_client.source, 'volume'):
            current_vol = int(ctx.voice_client.source.volume * 100)
        return await ctx.send(f"🔊 Volumen actual: **{current_vol}%**")

    if vol < 0 or vol > 200:
        return await ctx.send("❌ El volumen debe estar entre 0 y 200%.")

    guild_volumes[guild_id] = vol / 100

    if ctx.voice_client and ctx.voice_client.source and hasattr(ctx.voice_client.source, 'volume'):
        ctx.voice_client.source.volume = vol / 100

    await ctx.send(f"🔊 Volumen ajustado a **{vol}%**")


@bot.command(name='limpiar_cola', aliases=['eliminar_cola', 'borrar_cola', 'clear_queue', 'clearcola'])
async def clear_queue(ctx: commands.Context) -> None:
    """Limpia la cola de reproducción"""
    if ctx.guild.id in queues and queues[ctx.guild.id]:
        queues[ctx.guild.id].clear()
        await ctx.send("🗑️ Cola de reproducción borrada.")
    else:
        await ctx.send("📭 La cola ya está vacía.")


@bot.command(name='detente', aliases=['parar', 'stop', 'detener'])
async def stop(ctx: commands.Context):
    """Detiene la música y limpia la cola"""
    voice = ctx.voice_client

    if not voice or not voice.is_playing():
        return await ctx.send("⚠️ No hay música reproduciéndose")

    # Detener la sesión DJ si está activa
    if ctx.guild.id in dj_sessions:
        dj_sessions[ctx.guild.id]["active"] = False
        task = dj_auto_tasks.pop(ctx.guild.id, None)
        if task and not task.done():
            task.cancel()

    # Limpiar la cola primero
    if ctx.guild.id in queues:
        queues[ctx.guild.id].clear()

    # Detener la reproducción
    voice.stop()

    # Resetear la canción actual de este servidor
    current_songs.pop(ctx.guild.id, None)

    await ctx.send("⏹️ Música detenida y cola limpiada")


@bot.command(name='reproducir_primero', aliases=['primero', 'playtop', 'top'])
async def playtop(ctx: commands.Context, *, busqueda: str) -> None:
    """Añade una canción al inicio de la cola"""
    if not ctx.author.voice:
        return await ctx.send("❌ ¡No estás en un canal de voz!")

    last_voice_channel_ids[ctx.guild.id] = ctx.author.voice.channel.id

    try:
        voice_client = ctx.voice_client or await safe_connect(ctx.author.voice.channel)
        song = await resolve_song(busqueda, ctx.author)
        await add_song_to_queue(ctx.guild.id, song, front=True)

        if not voice_client.is_playing() and not voice_client.is_paused():
            await check_queue(ctx)
            return

        await ctx.send(f"⏫ Canción añadida al inicio de la cola: **{song['title']}**")

    except Exception as e:
        logger.error(f"Error en playtop: {traceback.format_exc()}")
        await ctx.send(f"❌ Error: {str(e)[:200]}")


@bot.command(name='guardar_playlist', aliases=['guarda', 'guarda_lista', 'guardar_cola'])
async def save_playlist_cmd(ctx: commands.Context, playlist_name: str, *, option: str = None) -> None:
    """Guarda la cola actual o el historial como playlist"""
    user_id = str(ctx.author.id)

    if option and option.lower() == 'hoy':
        # Guardar historial de hoy como playlist
        history = load_history(ctx.guild.id)
        if not history:
            return await ctx.send("❌ No hay historial de hoy para guardar.")

        count = save_playlist(user_id, playlist_name, history)
        await ctx.send(f"✅ Playlist **{playlist_name}** creada con {count} canciones únicas del historial de hoy.")
    else:
        # Guardar cola actual como playlist
        if ctx.guild.id not in queues or not queues[ctx.guild.id]:
            return await ctx.send("❌ La cola está vacía.")

        count = save_playlist(user_id, playlist_name, queues[ctx.guild.id])
        await ctx.send(f"✅ Playlist **{playlist_name}** creada con {count} canciones únicas de la cola actual.")


@bot.command(name='cargar_playlist', aliases=['cargar', 'musica', 'musicas_guardadas'])
async def load_playlist_cmd(ctx: commands.Context, playlist_name: str) -> None:
    """Carga una playlist a la cola de reproducción"""
    user_id = str(ctx.author.id)
    playlist = load_playlist(user_id, playlist_name)

    if not playlist:
        return await ctx.send(f"❌ No se encontró la playlist **{playlist_name}**.")

    # Añadir canciones a la cola
    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = []

    added_count = 0
    for song in playlist:
        if not any(s['url'] == song['url'] for s in queues[ctx.guild.id]):
            queues[ctx.guild.id].append(song)
            added_count += 1

    if added_count == 0:
        return await ctx.send("ℹ️ Todas las canciones de la playlist ya están en la cola.")

    embed = discord.Embed(
        title=f"🎶 Playlist cargada: {playlist_name}",
        description=f"Se añadieron {added_count} canciones a la cola.",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

    # Si no hay nada reproduciéndose, iniciar la reproducción
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await check_queue(ctx)


@bot.command(name='listar_playlists')
async def list_playlists(ctx: commands.Context) -> None:
    """Muestra todas las playlists del usuario"""
    user_id = str(ctx.author.id)
    playlists = get_user_playlists(user_id)

    if not playlists:
        return await ctx.send("📭 No tienes ninguna playlist guardada.")

    embed = discord.Embed(
        title="📋 Tus Playlists",
        description="\n".join(f"• {name}" for name in playlists),
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)


@bot.command(name='historial')
async def show_history(ctx: commands.Context, date: Optional[str] = None) -> None:
    """Muestra el historial de reproducción"""
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return await ctx.send("❌ Formato de fecha inválido. Usa YYYY-MM-DD.")

    history = load_history(ctx.guild.id, date)

    if not history:
        date_msg = f"el {date}" if date else "hoy"
        return await ctx.send(f"📭 No hay historial de reproducción para {date_msg}.")

    date_display = date if date else "hoy"
    embed = discord.Embed(
        title=f"📜 Historial de reproducción ({date_display})",
        description=f"Total: {len(history)} canciones",
        color=discord.Color.dark_gold()
    )

    # Mostrar las últimas 10 canciones
    for i, song in enumerate(history[-10:][::-1], 1):
        duration = ""
        if song.get('duration', 0) > 0:
            mins, secs = divmod(song['duration'], 60)
            duration = f" [{mins}:{secs:02d}]"

        embed.add_field(
            name=f"{len(history) - 10 + i}. {song['title']}{duration}",
            value=f"Solicitado por: {song['requested_by']}",
            inline=False
        )

    if len(history) > 10:
        embed.set_footer(text=f"Mostrando las últimas 10 de {len(history)} canciones")

    await ctx.send(embed=embed)

#----------------------------------
# Modulo del dj automatico
#----------------------------------

@bot.command(name='dj', aliases=['radio', 'mix'])
async def dj_mode(ctx: commands.Context, *, query: str = None):
    """Modo DJ profesional con reproducción automática y sugerencias inteligentes"""
    if not check_same_voice_channel(ctx):
        return await ctx.send("❌ Debes estar en el mismo canal de voz que el bot.")

    # 1. Actualizar actividad CORRECTAMENTE
    last_activity[ctx.guild.id] = time.time()  # ¡Así se hace!

    # 2. Configurar sesión DJ
    dj_config = {
        "active": True,
        "last_query": query,
        "auto_add": True,
        "theme": "default",
        "origin_channel": ctx.channel.id,  # Guardar solo el ID
        "last_add_time": time.time()
    }
    # Enviar mensaje de carga inicial con el GIF profesional
    loading_embed = discord.Embed(
        title="🎧 Iniciando Modo DJ...",
        description="**Preparando la mejor experiencia musical para ti**\n\n"
                    "⌛ Analizando tus preferencias y buscando canciones...",
        color=0x9147FF  # Color morado profesional
    )
    loading_embed.set_thumbnail(url="https://i.pinimg.com/originals/7b/1c/c2/7b1cc273a5db206f9e3e4f4d45c84f07.gif")
    loading_embed.set_footer(text="Archeon DJ System • Procesando tu solicitud")

    loading_msg = await ctx.send(embed=loading_embed)

    # Manejar comando stop
    if query and query.lower() == "stop":
        if ctx.guild.id in dj_sessions:
            dj_sessions[ctx.guild.id]["active"] = False
            await loading_msg.edit(embed=discord.Embed(
                title="🎧 Modo DJ Detenido",
                description="La música continuará hasta que la cola se acabe.",
                color=0x9147FF
            ))
        else:
            await loading_msg.edit(embed=discord.Embed(
                title="ℹ️ No hay DJ Activo",
                description="No hay una sesión DJ activa para detener.",
                color=0x7289DA
            ))
        return

    # Determinar tema basado en la consulta
    theme = "default"
    if query:
        query_lower = query.lower()
        if any(word in query_lower for word in ["fiesta", "party", "baile"]):
            theme = "party"
        elif any(word in query_lower for word in ["relax", "chill", "tranquilo"]):
            theme = "chill"
        elif any(word in query_lower for word in ["gym", "workout", "ejercicio"]):
            theme = "workout"
        elif any(word in query_lower for word in ["focus", "concentración", "estudio"]):
            theme = "focus"

    # Iniciar/continuar sesión DJ
    if ctx.guild.id not in dj_sessions:
        dj_sessions[ctx.guild.id] = {
            "active": True,
            "last_query": query if query else None,
            "auto_add": True,
            "theme": theme,
            "origin_channel": ctx.channel,
            "last_add_time": time.time()
        }
    else:
        dj_sessions[ctx.guild.id].update({
            "active": True,
            "auto_add": True,
            "theme": theme,
            "last_query": query if query else dj_sessions[ctx.guild.id]["last_query"],
            "last_add_time": time.time()
        })

    # Si no hay query, generar una basada en el historial
    if not query:
        if not current_songs.get(ctx.guild.id) and not queues.get(ctx.guild.id):
            return await loading_msg.edit(embed=discord.Embed(
                title="🎧 Se necesita una canción inicial",
                description="Usa `¡dj [canción/artista]` para comenzar o reproduce algo primero",
                color=0x7289DA
            ))

        try:
            history = load_history(ctx.guild.id)
            if not history:
                return await loading_msg.edit(embed=discord.Embed(
                    title="🎧 Historial Insuficiente",
                    description="No tengo suficiente historial para hacer sugerencias. Prueba con `¡dj [canción/artista]`",
                    color=0x7289DA
                ))

            # Mejorar el prompt para la IA
            prompt = (
                    "Analiza estas canciones recientes:\n" +
                    "\n".join(f"- {song['title']}" for song in history[-5:]) +
                    "\n\nComo DJ experto, genera un término de búsqueda que represente una continuación natural de esta sesión musical. "
                    "Considera género, estado de ánimo y artistas similares. "
                    "Devuelve SOLO el término de búsqueda, nada más."
            )

            ai_query = await safe_gemini_text(prompt, context="DJ sugerencia prefijo")
            query = ai_query or f"{history[-1].get('title', '')} radio mix"
            dj_sessions[ctx.guild.id]["last_query"] = query

            await loading_msg.edit(embed=discord.Embed(
                title="🎧 Sugerencia Automática del DJ",
                description=f"Basado en tu historial, continuaré con: **{query}**",
                color=0x9147FF
            ))

        except Exception as e:
            logger.error(f"Error generando sugerencia DJ: {e}")
            return await loading_msg.edit(embed=discord.Embed(
                title="❌ Error al Generar Sugerencia",
                description="No pude generar una sugerencia automática. Prueba con `¡dj [canción/artista]`",
                color=0xE74C3C
            ))

    # Procesar la búsqueda con IA mejorada
    try:
        # Paso 1: Obtener términos de búsqueda mejorados
        prompt = (
            f"Como DJ musical experto, analiza esta consulta: '{query}'\n"
            "Genera 3 términos de búsqueda específicos para encontrar música perfectamente relacionada en YouTube.\n"
            "Considera:\n"
            "- Artistas similares\n"
            "- Géneros relacionados\n"
            "- Estados de ánimo\n"
            "- 'Radio de...' o 'Mix de...'\n"
            "- Canciones específicas que encajen\n\n"
            "Formato: término1 | término2 | término3 (sin explicaciones)"
        )

        ai_terms = await safe_gemini_text(prompt, context="DJ términos prefijo")
        if ai_terms:
            search_terms = [term.strip() for term in ai_terms.split('|') if term.strip()][:4]
        else:
            search_terms = build_dj_fallback_terms(query, limit=5)

        if not search_terms:
            search_terms = build_dj_fallback_terms(query, limit=5)

        # Paso 2: Buscar y añadir a la cola con mejor manejo
        added_songs = 0
        songs_added = []
        exclusions = get_dj_exclusion_markers(ctx.guild.id)

        for term in search_terms:
            try:
                song = await resolve_song(term, ctx.author, max_duration=600, exclude_markers=exclusions)

                added = await add_song_to_queue(ctx.guild.id, song, avoid_recent=True)
                if not added:
                    continue

                exclusions.update(song_markers(song))
                added_songs += 1
                songs_added.append(song)
                await asyncio.sleep(1)

            except Exception as e:
                logger.warning(f"No se pudo añadir término DJ {term}: {e}")
                continue

        if added_songs == 0:
            return await loading_msg.edit(embed=discord.Embed(
                title="❌ No se encontraron canciones",
                description="No pude encontrar música nueva para añadir. Prueba con otro término.",
                color=0xE74C3C
            ))

        # Paso 3: Manejar reproducción
        voice_client = ctx.voice_client or await safe_connect(ctx.author.voice.channel)

        if not voice_client.is_playing() and not voice_client.is_paused():
            await check_queue(ctx)

        # Embed de resultados profesional
        theme_colors = {
            "default": 0x9147FF,
            "party": 0xFF00FF,
            "chill": 0x00BFFF,
            "workout": 0xFF4500,
            "focus": 0x32CD32
        }

        embed = discord.Embed(
            title=f"🎧 Modo DJ Activado - {theme.capitalize()}",
            description=f"**Tema seleccionado:** {query}\n"
                        f"**Canciones añadidas:** {added_songs}\n"
                        f"**Modo:** Auto-reproducción activada",
            color=theme_colors.get(theme, 0x9147FF)
        )

        # Mostrar las próximas canciones
        for i, song in enumerate(songs_added[:3], 1):
            duration = f"{song['duration'] // 60}:{song['duration'] % 60:02d}" if song[
                                                                                      'duration'] > 0 else "Desconocida"
            embed.add_field(
                name=f"🎵 Próxima {i}",
                value=f"[{song['title']}]({song['web_url']})\n"
                      f"**Artista:** {song['uploader']}\n"
                      f"**Duración:** {duration}",
                inline=False
            )

        embed.set_thumbnail(url=songs_added[0]['thumbnail'] if songs_added else None)
        embed.set_footer(
            text=f"DJ: {ctx.author.display_name} | Usa ¡dj stop para finalizar",
            icon_url=ctx.author.display_avatar.url
        )

        await loading_msg.edit(embed=embed)

        # Iniciar sistema de auto-adición
        if dj_sessions[ctx.guild.id]["auto_add"]:
            ensure_dj_auto_task(ctx.guild)

    except Exception as e:
        logger.error(f"Error en modo DJ: {e}")
        await loading_msg.edit(embed=discord.Embed(
            title="❌ Error en Modo DJ",
            description=f"Ocurrió un error: {str(e)[:200]}",
            color=0xE74C3C
        ))


async def auto_add_songs_task(guild):
    """Añade canciones automáticamente cuando la cola está por terminarse"""
    while guild.id in dj_sessions and dj_sessions[guild.id]["active"]:
        try:
            # Esperar condiciones óptimas
            while True:
                # Verificar si el modo DJ sigue activo
                if guild.id not in dj_sessions or not dj_sessions[guild.id]["active"]:
                    return

                # Verificar estado de la cola
                queue_length = len(queues.get(guild.id, []))
                voice_client = guild.voice_client

                # Condiciones para añadir más canciones:
                # 1. Quedan menos de 2 canciones en cola O
                # 2. No hay nada reproduciéndose actualmente
                if queue_length <= 2 or (
                        voice_client and not voice_client.is_playing() and not voice_client.is_paused()):
                    break

                await asyncio.sleep(5)

            # Obtener contexto simulado
            class SimulatedContext:
                def __init__(self, guild):
                    self.guild = guild
                    self.author = guild.me
                    self.voice_client = guild.voice_client

                @property
                def channel(self):
                    return dj_sessions.get(guild.id, {}).get("origin_channel") or guild.system_channel or next(
                        (c for c in guild.text_channels
                         if c.permissions_for(guild.me).send_messages), None)

            ctx = SimulatedContext(guild)
            query = dj_sessions[guild.id]["last_query"]

            if not query:
                # Generar sugerencia basada en historial
                history = load_history(guild.id)
                if history:
                    prompt = (
                            "Analiza estas canciones recientes:\n" +
                            "\n".join(f"- {song['title']}" for song in history[-5:]) +
                            "\n\nComo DJ experto, genera un término de búsqueda que represente una continuación natural de esta sesión musical. "
                            "Devuelve SOLO el término de búsqueda, nada más."
                    )
                    ai_query = await safe_gemini_text(prompt, context="DJ auto sugerencia")
                    query = ai_query or f"{history[-1].get('title', '')} radio mix"
                    dj_sessions[guild.id]["last_query"] = query

            if query:
                # Generar términos de búsqueda
                prompt = (
                    f"Como DJ musical experto, analiza esta consulta: '{query}'\n"
                    "Genera 3 términos de búsqueda específicos para encontrar música perfectamente relacionada en YouTube.\n"
                    "Formato: término1 | término2 | término3 (sin explicaciones)"
                )
                ai_terms = await safe_gemini_text(prompt, context="DJ auto términos")
                if ai_terms:
                    search_terms = [term.strip() for term in ai_terms.split('|') if term.strip()][:4]
                else:
                    search_terms = build_dj_fallback_terms(query, limit=5)

                if not search_terms:
                    search_terms = build_dj_fallback_terms(query, limit=5)

                # Procesar cada término de búsqueda sin repetir lo que ya sonó
                added_songs = 0
                exclusions = get_dj_exclusion_markers(guild.id)
                for term in search_terms:
                    try:
                        song = await resolve_song(term, guild.me, max_duration=600, exclude_markers=exclusions)

                        added = await add_song_to_queue(guild.id, song, avoid_recent=True)
                        if not added:
                            continue

                        exclusions.update(song_markers(song))
                        added_songs += 1
                        await asyncio.sleep(1)

                    except Exception as e:
                        logger.warning(f"No se pudo auto-añadir término DJ {term}: {e}")
                        continue

                if added_songs > 0 and ctx.channel:
                    try:
                        await ctx.channel.send(
                            f"🎧 Modo DJ: Añadí {added_songs} nuevas canciones basadas en **{query}**"
                        )
                    except Exception as e:
                        logger.error(f"Error enviando mensaje de auto-add: {e}")

        except Exception as e:
            logger.error(f"Error en auto_add_songs_task: {e}")

        # Esperar antes de verificar nuevamente
        await asyncio.sleep(10)


# --------------------------
# Módulo de IA
# --------------------------

@bot.command()
async def charla(ctx, *, mensaje: str):
    """Interactúa con la IA de Google Gemini con memoria contextual mejorada."""
    user_id = str(ctx.author.id)

    # Respuestas rápidas
    quick_responses = {
        "¿cómo te llamas?": "🤖 ¡Soy Archeon, tu asistente de Discord! ✨",
        "¿quién eres?": "🤖 ¡Soy Archeon, tu asistente de Discord! ✨",
        "¿cuál es tu nombre?": "🤖 ¡Soy Archeon, tu asistente de Discord! ✨",
        "¿quién soy?": f"🤖 ¡Claro que te conozco, {ctx.author.mention}! Eres {ctx.author.name} 😊",
        "¿cómo me llamo?": f"🤖 ¡Claro que te conozco, {ctx.author.mention}! Eres {ctx.author.name} 😊",
        "¿me conoces?": f"🤖 ¡Claro que te conozco, {ctx.author.mention}! Eres {ctx.author.name} 😊"
    }

    lower_msg = mensaje.lower().strip()
    if lower_msg in quick_responses:
        return await ctx.send(quick_responses[lower_msg])

    try:
        # Inicializar historial si es nuevo usuario
        if user_id not in chat_histories:
            chat_histories[user_id] = []

        # Construir contexto
        context = {
            "historial": "\n".join(chat_histories[user_id][-MAX_HISTORY:]),
            "nuevo_mensaje": mensaje,
            "usuario": ctx.author.name
        }

        prompt = (
            "Eres Archeon, un asistente virtual inteligente que conversa por Discord. "
            "Recuerdas información relevante de interacciones anteriores para ofrecer respuestas personalizadas. "
            "Puedes guardar detalles como nombre, intereses, estilo de conversación del usuario, temas frecuentes y tareas pendientes.\n\n"
            "Historial reciente de la conversación:\n"
            "{historial}\n\n"
            "Nuevo mensaje de {usuario}:\n"
            "{nuevo_mensaje}\n\n"
            "Con base en lo que recuerdas del usuario y del historial reciente, responde de forma clara, útil y amigable. "
            "Si no tienes suficiente contexto o necesitas confirmar algo, haz una pregunta breve. "
            "Evita repetir información innecesaria y adapta tu estilo si detectas un cambio en el tono del usuario."
        ).format(**context)

        # Generar respuesta
        if model is None:
            return await ctx.send("❌ Falta configurar `GOOGLE_API_KEY` en el archivo `.env`.")
        response = model.generate_content(prompt)
        respuesta = response.text.strip()

        # Actualizar historial
        chat_histories[user_id].extend([
            f"{ctx.author.name}: {mensaje}",
            f"Archeon: {respuesta}"
        ])
        chat_histories[user_id] = chat_histories[user_id][-MAX_HISTORY:]

        # Enviar respuesta
        await ctx.send(f"{ctx.author.mention} {respuesta}")


    except genai.types.generation_types.BlockedPromptException as blocked_error:
        await ctx.send("🚫 Lo siento, no puedo procesar ese tipo de contenido.")
        logger.error(f"Contenido bloqueado: {blocked_error}")


    except Exception as e:
        if "GoogleAPIError" in str(type(e)):  # Manejo genérico de errores de API
            await ctx.send("🔴 Error con la API de Google. Por favor, reporta esto al administrador.")
            logger.error(f"Google API Error: {e}")
        else:
            logger.error(f"Error inesperado: {e}", exc_info=True)
            await ctx.send("⚠️ Ocurrió un error inesperado. Por favor, intenta nuevamente más tarde.")



# --------------------------
# --------------------------
# Helpers de roasts/insultos de broma
# --------------------------

ROASTS_FUERTES = [
    "eres tan manco que el tutorial pidió que lo saltaras por dignidad.",
    "tu jugada tuvo menos futuro que cargador mordido por perro.",
    "vienes tan perdido que hasta el minimapa se puso en modo duelo.",
    "tienes la puntería de una impresora sin tinta: haces ruido y no sale nada útil.",
    "tu plan salió tan torcido que hasta el bug pidió no ser asociado contigo.",
    "si fueras build, producción te rechaza antes de compilar y QA te usa como leyenda urbana.",
    "das tanta confianza como botón verde de descarga en página con veinte popups.",
    "tu lógica hace tanto ruido que el debugger se desconectó por salud mental.",
    "eres el tipo de error que aparece solo cuando el profesor está mirando.",
    "jugaste tan mal que el lag presentó una queja formal por difamación.",
    "tienes menos timing que NPC cruzando la calle en misión de sigilo.",
    "tu estrategia fue tan brillante que apagó el servidor para no verla más.",
    "eres como Wi‑Fi de terminal: prometes conexión y entregas sufrimiento.",
    "tu intento fue tan triste que hasta el botón de retry sintió vergüenza.",
    "si el fracaso diera XP, ya estarías en nivel leyenda.",
    "eres más paquete que archivo .zip corrupto: pesas, molestas y no sirves cuando te abren.",
    "tu cerebro hizo buffering y aun así entregó contenido en 144p.",
    "vienes con tanta seguridad para fallar que pareces demo técnica de desastre.",
    "tu presencia en la partida baja el MMR moral del equipo completo.",
    "eres la razón por la que los tutoriales ahora traen subtítulos y dibujos.",
    "tu idea fue tan mala que el historial del navegador pidió borrarse solo.",
    "tienes menos impacto que golpe con servilleta mojada.",
    "si fueras parche, arreglas un bug y creas siete desgracias nuevas.",
    "tu nivel de juego parece prueba gratuita vencida.",
    "la IA te analizó y pidió volver a entrenamiento básico.",
    "tu argumento llegó tan débil que hasta el eco le ganó el debate.",
    "eres como captcha mal hecho: estorbas y nadie entiende para qué estás.",
    "tu skill está tan escondida que ni con cookies.txt la encuentra yt-dlp.",
    "fallaste con tanta pasión que casi parece una carrera profesional.",
    "tu combo fue tan triste que el mando vibró por lástima.",
    "eres el DLC que nadie compró y aun así arruinó el juego base.",
    "tu desempeño tiene más caídas que servidor barato en día de examen.",
    "si fueras alarma, no despiertas a nadie y encima das coraje.",
    "tu jugada fue tan mala que el árbitro quiso pedir perdón por verla.",
    "eres la prueba de que a veces el modo fácil también necesita modo fácil."
]

ROAST_PROHIBITED_PATTERNS = [
    r"\bmadre\b", r"\bpadre\b", r"\bfamilia\b", r"\braza\b", r"\brelig", r"\bgay\b",
    r"\bdiscap", r"\benferm", r"\bmu[eé]rete\b", r"\bsuic", r"\bviol", r"\bpobre\b",
    r"\bnegro\b", r"\bindio\b", r"\bmaric", r"\bputa\b", r"\bputo\b"
]


def sanitize_roast_text(text_value: str, target: discord.Member) -> str:
    """Evita duplicar nombre/mención y baja salidas que se pasan de la raya."""
    cleaned = (text_value or "").strip()

    blocked_parts = {
        target.mention,
        f"<@{target.id}>",
        f"<@!{target.id}>",
        f"@{target.display_name}",
        f"@{target.name}",
        target.display_name,
        target.name,
        getattr(target, "global_name", None) or "",
    }

    for part in sorted((p for p in blocked_parts if p), key=len, reverse=True):
        cleaned = re.sub(re.escape(part), "", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"^(aquí tienes|claro|vale|ok|roast|insulto|respuesta)\s*[:,\-–—]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,:;-–—\n\t")
    cleaned = re.sub(r"^[,.:;¡!¿?\s]+", "", cleaned)

    # Si la IA se va a cosas sensibles, se reemplaza por fallback fuerte pero de broma.
    lowered = cleaned.lower()
    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in ROAST_PROHIBITED_PATTERNS):
        cleaned = random.choice(ROASTS_FUERTES)

    if not cleaned or len(cleaned) < 8:
        cleaned = random.choice(ROASTS_FUERTES)

    words = cleaned.split()
    if len(words) > 38:
        cleaned = " ".join(words[:38]).rstrip(" ,.;:") + "."

    return cleaned


async def generar_roast_limpio(usuario: discord.Member, razon: Optional[str] = None) -> str:
    """Roast pesado de confianza, con bypass de moderación del comando, sin duplicar el nombre."""
    fallback = random.choice(ROASTS_FUERTES)

    if model is None:
        return fallback

    try:
        prompt = (
            "Genera UN roast/insulto de broma entre amigos para un chat de Discord latino. "
            "Debe ser más feo, venenoso, humillante y gracioso, estilo gamer competitivo, pero sin odio real. "
            "No uses nombre, apodo ni mención del usuario porque el bot ya lo menciona afuera. "
            "No repitas ningún nombre propio. "
            "No menciones avatar, VC, canal de voz, comandos, Discord ni que eres IA. "
            "No ataques familia, origen, raza, religión, orientación, identidad, discapacidad, salud, pobreza o cuerpo. "
            "No uses amenazas reales, autolesión, doxxing ni sexualidad explícita. "
            f"Motivo: {razon or 'sin motivo específico'}. "
            "Máximo 26 palabras. Responde solo el insulto, directo, sin explicación."
        )
        response = model.generate_content(prompt)
        return sanitize_roast_text((response.text or "").strip(), usuario)
    except Exception as e:
        logger.warning(f"No pude generar roast con IA, uso fallback: {e}")
        return fallback


@bot.command(name="insultar", aliases=["roast", "quemar"])
async def insultar(ctx, usuario: discord.Member, *, razon=None):
    """Insulto/roast de broma fuerte. Este comando se salta la moderación automática del mensaje que lo invoca."""
    bypass_messages.add(ctx.message.id)

    try:
        insulto = await generar_roast_limpio(usuario, razon)
        await ctx.send(
            f"{usuario.mention}, {insulto}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )

    except Exception as e:
        logger.error(f"Error al generar roast: {e}")
        await ctx.send(
            f"{usuario.mention}, {sanitize_roast_text(random.choice(ROASTS_FUERTES), usuario)}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )


@bot.command()
async def olvidar(ctx):
    """Reinicia el historial de conversación contigo"""
    user_id = str(ctx.author.id)
    if user_id in chat_histories:
        chat_histories[user_id] = []
    await ctx.send("🔄 ¡He reiniciado nuestra conversación! ¿En qué puedo ayudarte ahora?")

@bot.command()
async def imagen(ctx, *, descripcion: str):
    """Genera una imagen a partir de una descripción en español usando Stability AI."""
    if not STABILITY_API_KEY:
        return await ctx.send("❌ Falta configurar `STABILITY_API_KEY` en el archivo `.env`.")

    await ctx.send(f"🎨 Generando imagen para: `{descripcion}`... Esto puede tardar unos segundos ⏳")

    try:
        descripcion_en = GoogleTranslator(source='es', target='en').translate(descripcion)
        logger.info(f"Prompt traducido para Stability AI: {descripcion_en}")

        payload = {
            "text_prompts": [{"text": descripcion_en, "weight": 1.0}],
            "cfg_scale": 7,
            "height": 512,
            "width": 512,
            "samples": 1,
            "steps": 30
        }
        headers = {
            "Authorization": f"Bearer {STABILITY_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            async with session.post(
                "https://api.stability.ai/v1/generation/stable-diffusion-v1-6/text-to-image",
                headers=headers,
                json=payload
            ) as response:
                try:
                    data = await response.json()
                except Exception:
                    data = {"message": await response.text()}

                if response.status != 200:
                    error_msg = data.get("message", "Error desconocido")
                    logger.error(f"Error en Stability API: {error_msg}")
                    return await ctx.send(f"❌ Error en la API: {error_msg}")

        if "artifacts" not in data or not data["artifacts"]:
            return await ctx.send("⚠️ No se pudo generar la imagen. Prueba con otra descripción.")

        image_base64 = data["artifacts"][0]["base64"]
        image_data = base64.b64decode(image_base64)

        with io.BytesIO(image_data) as image_buffer:
            file = discord.File(fp=image_buffer, filename="imagen.png")
            await ctx.send(file=file)

    except Exception as e:
        logger.error(f"Error al generar imagen: {traceback.format_exc()}")
        await ctx.send("❌ Ocurrió un error al generar la imagen. Verifica la descripción o intenta más tarde.")



# --------------------------
# Módulo de Diversión
# --------------------------

HUG_GIFS = [
    "https://media.tenor.com/0vl21YIsGvgAAAAC/hug-anime.gif",
    "https://media.tenor.com/9e1aE_xBLCsAAAAC/anime-hug.gif",
    "https://media.tenor.com/2lr9uM5JmPQAAAAC/hug.gif"
]

MEME_FALLBACKS = [
    ("Cuando el bot funciona a la primera", "https://i.imgflip.com/30b1gx.jpg"),
    ("Yo viendo el error y fingiendo calma", "https://i.imgflip.com/1bij.jpg"),
    ("El código: funciona. Yo: no lo toques.", "https://i.imgflip.com/26am.jpg")
]

EIGHT_BALL_RESPONSES = [
    "Sí, pero no lo presumas mucho.",
    "No. Y mi bola de cristal pidió vacaciones.",
    "Probablemente sí.",
    "Probablemente no.",
    "Hazlo, pero guarda backup como persona decente.",
    "Pregunta otra vez cuando Mercurio deje de hacer lag.",
    "Mi respuesta es sí, con cara de sospecha.",
    "Ni idea, pero suena a plan de viernes."
]

JOKES = [
    "¿Por qué el programador confundió Halloween con Navidad? Porque OCT 31 == DEC 25.",
    "Un SQL entra a un bar, se acerca a dos mesas y pregunta: ¿puedo hacer un JOIN?",
    "Mi código no tiene bugs, tiene decisiones creativas.",
    "El Wi‑Fi y yo tenemos algo en común: cuando más me necesitan, me caigo."
]


# Memes 100% en español: se generan con texto español usando memegen.link.
# Así no dependemos de títulos aleatorios en inglés de APIs externas.
SPANISH_MEME_TEMPLATES = [
    ("drake", "Cuando dicen que no toque el código", "Yo tocándolo igual porque 'solo es una cosita'"),
    ("twobuttons", "Arreglar un bug", "Crear tres nuevos", "Programador promedio"),
    ("disastergirl", "Yo después de ejecutar el bot", "El server viendo 32 comandos nuevos"),
    ("rollsafe", "No hay errores de sintaxis", "si nunca miras la consola"),
    ("gru", "Hacer un bot", "Añadir música", "Añadir IA", "Terminar creando Skynet con sueño"),
    ("doge", "Bot antes", "Bot ahora con memes en español"),
    ("fry", "No sé si el bot está mejorando", "o ya está agarrando conciencia"),
    ("success", "Compiló a la primera", "milagro certificado por San Backup"),
    ("bad", "El bot no falló", "solo decidió probar tu paciencia"),
    ("buzz", "Comandos slash", "comandos slash en español por todos lados"),
]

SHIP_COMMENTS_ES = [
    (0, 10, "Esto tiene menos futuro que un pendrive mojado."),
    (11, 25, "Hay química, pero de laboratorio clausurado."),
    (26, 45, "Podría funcionar si ambos bajan los requisitos gráficos."),
    (46, 65, "Hay chance, pero no canten victoria que Discord escucha."),
    (66, 80, "Uy, aquí huele a dúo dinámico y drama de servidor."),
    (81, 95, "Esto está tan fuerte que Cupido pidió moderador."),
    (96, 100, "Compatibilidad legendaria: ya mismo les cae parche de balance.")
]


def _meme_title(template: tuple) -> str:
    """Título corto del meme, siempre en español."""
    return " / ".join(str(part) for part in template[1:])


def _load_card_font(size: int, bold: bool = False):
    """Carga una fuente común sin depender de archivos incluidos en el proyecto."""
    try:
        from PIL import ImageFont
        font_candidates = [
            r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\seguisb.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
        for font_path in font_candidates:
            if font_path and os.path.exists(font_path):
                return ImageFont.truetype(font_path, size)
        return ImageFont.load_default()
    except Exception:
        return None


def _draw_wrapped_center(draw, text: str, box, font, fill=(20, 20, 20), spacing: int = 8):
    """Dibuja texto centrado y con salto de línea dentro de una caja."""
    if font is None:
        return

    x1, y1, x2, y2 = box
    max_width = max(10, x2 - x1 - 40)
    words = str(text).split()
    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    # Si el texto queda enorme, recorta líneas con elegancia.
    lines = lines[:4]
    heights = []
    widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])

    total_h = sum(heights) + spacing * max(0, len(lines) - 1)
    y = y1 + ((y2 - y1) - total_h) // 2

    for i, line in enumerate(lines):
        w = widths[i]
        h = heights[i]
        x = x1 + ((x2 - x1) - w) // 2
        draw.text((x, y), line, font=font, fill=fill)
        y += h + spacing


def build_spanish_meme_file() -> tuple[str, discord.File]:
    """
    Genera un meme REAL como imagen PNG en memoria.
    Ya no depende de URLs externas que Discord puede no previsualizar.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        raise RuntimeError("Falta Pillow para generar memes con imagen. Instala con: python -m pip install -U Pillow") from e

    template = random.choice(SPANISH_MEME_TEMPLATES)
    title = _meme_title(template)
    parts = [str(part) for part in template[1:]]

    width, height = 1000, 720
    palettes = [
        ((21, 25, 40), (255, 70, 130), (255, 255, 255)),
        ((18, 31, 45), (70, 180, 255), (255, 255, 255)),
        ((35, 24, 42), (255, 190, 70), (255, 255, 255)),
        ((22, 42, 34), (100, 255, 170), (255, 255, 255)),
    ]
    bg, accent, white = random.choice(palettes)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Fondo con patrón simple, para que no parezca puro texto triste.
    for i in range(0, width, 70):
        draw.line((i, 0, i - 220, height), fill=tuple(max(0, c - 10) for c in bg), width=3)

    # Marco principal.
    draw.rounded_rectangle((35, 35, width - 35, height - 35), radius=34, outline=accent, width=8)
    draw.rounded_rectangle((60, 60, width - 60, 145), radius=24, fill=accent)

    title_font = _load_card_font(42, bold=True)
    big_font = _load_card_font(52, bold=True)
    mid_font = _load_card_font(43, bold=True)
    small_font = _load_card_font(26, bold=False)

    _draw_wrapped_center(draw, "MEME EN ESPAÑOL", (70, 65, width - 70, 140), title_font, fill=(20, 20, 20))

    # Cajas de texto según cantidad de partes.
    usable_top = 175
    usable_bottom = height - 105
    gap = 18
    count = max(1, len(parts))
    box_h = (usable_bottom - usable_top - gap * (count - 1)) // count

    for idx, part in enumerate(parts):
        y1 = usable_top + idx * (box_h + gap)
        y2 = y1 + box_h
        fill_color = (245, 245, 245) if idx % 2 == 0 else (232, 238, 255)
        draw.rounded_rectangle((80, y1, width - 80, y2), radius=26, fill=fill_color)
        draw.rounded_rectangle((80, y1, width - 80, y2), radius=26, outline=accent, width=4)

        font = big_font if count <= 2 else mid_font
        _draw_wrapped_center(draw, part, (105, y1 + 10, width - 105, y2 - 10), font, fill=(15, 15, 20))

    # Footer.
    footer = "Archeon Bot • memes en español, no en traductor con sueño"
    bbox = draw.textbbox((0, 0), footer, font=small_font)
    draw.text(((width - (bbox[2] - bbox[0])) // 2, height - 82), footer, font=small_font, fill=white)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return title, discord.File(buffer, filename="meme_espanol.png")


def build_spanish_meme() -> tuple[str, str]:
    """
    Compatibilidad vieja: devuelve un título y una URL.
    El comando nuevo usa build_spanish_meme_file() para mandar imagen real.
    """
    template = random.choice(SPANISH_MEME_TEMPLATES)
    return _meme_title(template), "https://i.imgur.com/8Km9tLL.png"


def _ship_comment(porcentaje: int) -> str:
    comentario = "El algoritmo del amor se fue por tacos."
    for min_v, max_v, text_comment in SHIP_COMMENTS_ES:
        if min_v <= porcentaje <= max_v:
            return text_comment
    return comentario


def _ship_name(usuario1, usuario2) -> str:
    nombre1 = str(usuario1.display_name or usuario1.name)
    nombre2 = str(usuario2.display_name or usuario2.name)
    return (nombre1[:max(1, len(nombre1)//2)] + nombre2[max(1, len(nombre2)//2):]).replace(" ", "")


def build_ship_embed(usuario1, usuario2, porcentaje: Optional[int] = None) -> discord.Embed:
    """Crea un ship más bonito, en español y con comentario según porcentaje."""
    porcentaje = random.randint(0, 100) if porcentaje is None else max(0, min(100, int(porcentaje)))
    comentario = _ship_comment(porcentaje)
    nombres = f"{usuario1.display_name} x {usuario2.display_name}"
    ship_name = _ship_name(usuario1, usuario2)

    embed = discord.Embed(
        title="💘 Mira qué tanto se quieren esos dos 😳",
        description=(
            f"**Pareja:** {nombres}\n"
            f"**Nombre del ship:** `{ship_name}`\n"
            f"**Compatibilidad:** **{porcentaje}%**\n\n"
            f"🗣️ {comentario}"
        ),
        color=0xFF4FA3
    )
    embed.set_footer(text="Shipómetro Archeon • 100% científico, como horóscopo con Wi‑Fi")
    try:
        embed.set_thumbnail(url=usuario1.display_avatar.url)
    except Exception:
        pass
    return embed


def build_ship_card_file(usuario1, usuario2, porcentaje: int) -> Optional[discord.File]:
    """Genera una imagen bonita del ship para que no salga una barra triste de texto."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    width, height = 1000, 520
    img = Image.new("RGB", (width, height), (32, 18, 42))
    draw = ImageDraw.Draw(img)

    # Fondo diagonal.
    for x in range(-height, width, 42):
        draw.line((x, 0, x + height, height), fill=(43, 24, 56), width=12)

    accent = (255, 79, 163)
    white = (255, 255, 255)
    soft = (255, 218, 235)

    draw.rounded_rectangle((35, 35, width - 35, height - 35), radius=36, outline=accent, width=8)
    draw.rounded_rectangle((70, 70, width - 70, 150), radius=28, fill=accent)

    title_font = _load_card_font(40, bold=True)
    name_font = _load_card_font(46, bold=True)
    percent_font = _load_card_font(86, bold=True)
    small_font = _load_card_font(27, bold=False)

    _draw_wrapped_center(draw, "MIRA QUÉ TANTO SE QUIEREN ESOS DOS", (85, 80, width - 85, 142), title_font, fill=(25, 15, 30))

    nombres = f"{usuario1.display_name}  ×  {usuario2.display_name}"
    _draw_wrapped_center(draw, nombres, (80, 175, width - 80, 240), name_font, fill=white)

    # Barra de amor.
    bar_x1, bar_y1, bar_x2, bar_y2 = 115, 300, 885, 365
    draw.rounded_rectangle((bar_x1, bar_y1, bar_x2, bar_y2), radius=30, fill=(70, 50, 82))
    filled_w = int((bar_x2 - bar_x1) * max(0, min(100, porcentaje)) / 100)
    if filled_w > 0:
        draw.rounded_rectangle((bar_x1, bar_y1, bar_x1 + filled_w, bar_y2), radius=30, fill=accent)

    pct_text = f"{porcentaje}%"
    bbox = draw.textbbox((0, 0), pct_text, font=percent_font)
    draw.text(((width - (bbox[2] - bbox[0])) // 2, 370), pct_text, font=percent_font, fill=soft)

    comentario = _ship_comment(porcentaje)
    _draw_wrapped_center(draw, comentario, (85, 455, width - 85, 500), small_font, fill=white)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return discord.File(buffer, filename="shipometro.png")


@bot.command(name='abrazo', aliases=['hug', 'abrazar'])
async def abrazo(ctx: commands.Context, usuario: Optional[discord.Member] = None):
    """Manda un abrazo virtual."""
    usuario = usuario or ctx.author
    embed = discord.Embed(
        title="🫂 Abrazo enviado",
        description=f"{ctx.author.mention} le mandó un abrazo a {usuario.mention}.",
        color=0xFFB6C1
    )
    embed.set_image(url=random.choice(HUG_GIFS))
    await ctx.send(embed=embed)


@bot.command(name='meme', aliases=['memazo'])
async def meme(ctx: commands.Context):
    """Genera un meme en español como imagen."""
    try:
        title, file = build_spanish_meme_file()
        await ctx.send(content=f"😂 **{title}**", file=file)
    except Exception as e:
        logger.error(f"Error generando meme con imagen: {traceback.format_exc()}")
        await ctx.send(f"❌ No pude generar la imagen del meme: {str(e)[:300]}")



@bot.command(name='chiste', aliases=['joke'])
async def chiste(ctx: commands.Context):
    """Cuenta un chiste corto."""
    await ctx.send(f"😄 {random.choice(JOKES)}")


@bot.command(name='moneda', aliases=['coinflip', 'caraocruz'])
async def moneda(ctx: commands.Context):
    """Lanza una moneda."""
    await ctx.send(f"🪙 Salió **{random.choice(['cara', 'cruz'])}**.")


@bot.command(name='dado', aliases=['roll'])
async def dado(ctx: commands.Context, caras: int = 6):
    """Lanza un dado."""
    caras = max(2, min(caras, 100))
    await ctx.send(f"🎲 D{caras}: **{random.randint(1, caras)}**")


@bot.command(name='bola8', aliases=['8ball', 'pregunta'])
async def bola8(ctx: commands.Context, *, pregunta: str):
    """Responde una pregunta estilo bola 8."""
    await ctx.send(f"🎱 **Pregunta:** {pregunta}\n**Respuesta:** {random.choice(EIGHT_BALL_RESPONSES)}")


@bot.command(name='elegir', aliases=['elige', 'choose'])
async def elegir(ctx: commands.Context, *, opciones: str):
    """Elige entre opciones separadas por coma."""
    partes = [op.strip() for op in opciones.split(',') if op.strip()]
    if len(partes) < 2:
        return await ctx.send("❌ Dame al menos 2 opciones separadas por coma. Ej: `¡elegir pizza, tacos, hamburguesa`")
    await ctx.send(f"🤔 Elijo: **{random.choice(partes)}**")


@bot.command(name='ship')
async def ship(ctx: commands.Context, usuario1: discord.Member, usuario2: Optional[discord.Member] = None):
    """Mira qué tanto se quieren esos dos 😳."""
    usuario2 = usuario2 or ctx.author
    porcentaje = random.randint(0, 100)
    embed = build_ship_embed(usuario1, usuario2, porcentaje)
    file = build_ship_card_file(usuario1, usuario2, porcentaje)
    if file:
        embed.set_image(url="attachment://shipometro.png")
        await ctx.send(embed=embed, file=file)
    else:
        await ctx.send(embed=embed)





# --------------------------
# Módulo Karaoke / Batalla de canto
# --------------------------
# Nota honesta: discord.py reproduce audio, mueve usuarios y maneja turnos.
# Para "detectar voz" real haría falta una librería de recepción de voz adicional; aquí dejamos scoring automático/manual.

KARAOKE_SESSIONS: Dict[int, Dict] = {}
KARAOKE_SCORE_MIN = 45
KARAOKE_SCORE_MAX = 100

KARAOKE_FALLBACK_COMMENTS_WINNER = [
    "cantó como si el server le debiera dinero y vino a cobrar con intereses.",
    "subió tanto el ego que hubo que ponerle límite de volumen.",
    "hoy no ganó: humilló con partitura y todo.",
    "se llevó la noche; ya casi pide camerino y agua sin gas."
]

KARAOKE_FALLBACK_COMMENTS_LOSER = [
    "tuvo una participación histórica: los grillos pidieron silencio.",
    "cantó con tanta fe que hasta el autotune renunció por agotamiento.",
    "si llueve mañana, no culpen al clima; culpen a esa nota final.",
    "hizo lo posible, y justamente ese fue el problema."
]


def _karaoke_get_session(guild_id: int) -> Optional[Dict]:
    return KARAOKE_SESSIONS.get(guild_id)


def _karaoke_user_entry(session: Dict, member_id: int) -> Optional[Dict]:
    for entry in session.get("queue", []):
        if entry.get("member_id") == member_id:
            return entry
    current = session.get("current")
    if current and current.get("member_id") == member_id:
        return current
    for entry in session.get("history", []):
        if entry.get("member_id") == member_id:
            return entry
    return None


def _karaoke_lyrics_url(song_title: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(f"{song_title} letra lyrics")


def _karaoke_score_bar(score: int) -> str:
    score = max(0, min(100, int(score)))
    filled = score // 10
    return "█" * filled + "░" * (10 - filled)


async def karaoke_ai_comment(member_name: str, score: int, winner: bool = False) -> str:
    fallback = random.choice(KARAOKE_FALLBACK_COMMENTS_WINNER if winner else KARAOKE_FALLBACK_COMMENTS_LOSER)
    if model is None:
        return fallback

    try:
        prompt = (
            "Genera un comentario corto, sarcástico y gracioso para una competencia de karaoke entre amigos. "
            "Debe ser de broma, estilo latino gamer, sin insultos sensibles, sin sexualidad explícita, sin amenazas reales. "
            f"Nombre visible del participante: {member_name}. Puntuación: {score}/100. "
            f"¿Es ganador?: {'sí' if winner else 'no'}. "
            "Máximo 22 palabras. Responde solo el comentario."
        )
        response = model.generate_content(prompt)
        comment = (response.text or "").strip()
        return comment if comment else fallback
    except Exception:
        return fallback


async def karaoke_move_participants(ctx: commands.Context, session: Dict) -> int:
    """Mueve a participantes al canal del host si el bot tiene permiso."""
    host_channel_id = session.get("host_channel_id")
    host_channel = ctx.guild.get_channel(host_channel_id) if host_channel_id else None
    if not isinstance(host_channel, discord.VoiceChannel):
        return 0

    moved = 0
    bot_member = ctx.guild.me
    if not bot_member.guild_permissions.move_members:
        return 0

    member_ids = set(session.get("participants", []))
    for member_id in member_ids:
        member = ctx.guild.get_member(member_id)
        if not member or member.bot or not member.voice or member.voice.channel == host_channel:
            continue
        try:
            await member.move_to(host_channel, reason="Modo karaoke Archeon")
            moved += 1
        except Exception as e:
            logger.warning(f"No pude mover a {member}: {e}")
    return moved


async def karaoke_show_results(ctx_or_interaction, session: Dict):
    guild = getattr(ctx_or_interaction, "guild", None)
    history = list(session.get("history", []))
    if session.get("current"):
        current = session["current"]
        current.setdefault("score", random.randint(KARAOKE_SCORE_MIN, KARAOKE_SCORE_MAX))
        history.append(current)

    if not history:
        return await send_context_message(ctx_or_interaction, "📭 No hay resultados todavía. Primero que alguien cante, aunque sea para espantar al algoritmo.")

    ranked = sorted(history, key=lambda e: int(e.get("score", 0)), reverse=True)
    winner_id = ranked[0].get("member_id")

    embed = discord.Embed(
        title="🏆 Resultados del Karaoke",
        description="Ranking final de la masacre musical:",
        color=discord.Color.gold()
    )

    for i, entry in enumerate(ranked[:10], 1):
        member = guild.get_member(entry.get("member_id")) if guild else None
        name = member.display_name if member else entry.get("display_name", "Participante")
        score = int(entry.get("score", 0))
        comment = entry.get("comment")
        if not comment:
            comment = await karaoke_ai_comment(name, score, winner=(entry.get("member_id") == winner_id))
        embed.add_field(
            name=f"#{i} • {name} — {score}/100",
            value=f"`{_karaoke_score_bar(score)}`\n🎵 {entry.get('song_title', 'Canción desconocida')}\n💬 {comment}",
            inline=False
        )

    await send_context_message(ctx_or_interaction, embed=embed)


async def karaoke_start_next(ctx_or_interaction):
    guild = getattr(ctx_or_interaction, "guild", None)
    if not guild:
        return
    session = _karaoke_get_session(guild.id)
    if not session or not session.get("running"):
        return

    # Guardar el actual en historial si viene de un stop/fin de canción.
    current = session.get("current")
    if current:
        current.setdefault("score", random.randint(KARAOKE_SCORE_MIN, KARAOKE_SCORE_MAX))
        member = guild.get_member(current.get("member_id"))
        display_name = member.display_name if member else current.get("display_name", "Participante")
        current.setdefault("comment", await karaoke_ai_comment(display_name, current["score"], winner=False))
        session.setdefault("history", []).append(current)
        session["current"] = None

    if not session.get("queue"):
        session["running"] = False
        await karaoke_show_results(ctx_or_interaction, session)
        return

    index = random.randrange(len(session["queue"]))
    entry = session["queue"].pop(index)
    session["current"] = entry

    member = guild.get_member(entry["member_id"])
    singer_text = member.mention if member else entry.get("display_name", "Participante")

    voice_client = guild.voice_client
    host_channel = guild.get_channel(session.get("host_channel_id"))
    if not voice_client and isinstance(host_channel, discord.VoiceChannel):
        try:
            voice_client = await safe_connect(host_channel)
        except Exception as e:
            await send_context_message(ctx_or_interaction, human_voice_error(e))
            return

    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await asyncio.sleep(0.4)

    embed = discord.Embed(
        title="🎤 Turno de Karaoke",
        description=(
            f"Le toca a {singer_text}\n"
            f"🎵 **[{entry['song_title']}]({entry['web_url']})**\n"
            f"📜 [Abrir letra aproximada]({_karaoke_lyrics_url(entry['song_title'])})\n\n"
            "El bot no puede compartir pantalla como usuario normal; dejo la letra en link y reproduzco la pista."
        ),
        color=0xFF2D95
    )
    if entry.get("thumbnail"):
        embed.set_thumbnail(url=entry["thumbnail"])
    embed.set_footer(text="Usa ¡karaoke puntuar @usuario 1-100 o espera a que acabe la canción.")
    await send_context_message(ctx_or_interaction, embed=embed)

    try:
        if voice_client:
            source = await create_audio_source(entry["url"], guild.id)
            voice_client.play(
                source,
                after=lambda e: asyncio.run_coroutine_threadsafe(karaoke_start_next(ctx_or_interaction), bot.loop)
            )
    except Exception as e:
        logger.error(f"Error reproduciendo karaoke: {traceback.format_exc()}")
        await send_context_message(ctx_or_interaction, f"❌ Error reproduciendo karaoke: {str(e)[:300]}")
        await karaoke_start_next(ctx_or_interaction)


@bot.group(name="karaoke", aliases=["k"], invoke_without_command=True)
async def karaoke_group(ctx: commands.Context):
    embed = discord.Embed(
        title="🎤 Modo Karaoke",
        description="Comandos disponibles:",
        color=0xFF2D95
    )
    embed.add_field(
        name="Flujo rápido",
        value=(
            "`¡karaoke iniciar` — crea sala karaoke en tu canal de voz\n"
            "`¡karaoke entrar nombre de canción` — te apuntas con canción\n"
            "`¡karaoke comenzar` — mueve participantes y empieza turnos aleatorios\n"
            "`¡karaoke puntuar @usuario 1-100 [comentario]` — puntuación manual\n"
            "`¡karaoke cola` — ver cola\n"
            "`¡karaoke finalizar` — muestra ranking final"
        ),
        inline=False
    )
    await ctx.send(embed=embed)


@karaoke_group.command(name="iniciar", aliases=["start", "crear"])
async def karaoke_iniciar(ctx: commands.Context):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("❌ Entra a un canal de voz primero para iniciar karaoke.")

    KARAOKE_SESSIONS[ctx.guild.id] = {
        "host_id": ctx.author.id,
        "host_channel_id": ctx.author.voice.channel.id,
        "participants": set([ctx.author.id]),
        "queue": [],
        "history": [],
        "current": None,
        "running": False,
        "created_at": time.time()
    }

    try:
        await safe_connect(ctx.author.voice.channel)
    except Exception as e:
        # No matamos el modo si falla voz; permite cargar cola y corregir dependencias.
        await ctx.send(human_voice_error(e))

    await ctx.send("🎤 Karaoke creado. Cada quien se apunta con `¡karaoke entrar nombre de canción`.")


@karaoke_group.command(name="entrar", aliases=["add", "agregar", "cancion", "canción"])
async def karaoke_entrar(ctx: commands.Context, *, cancion: str):
    session = _karaoke_get_session(ctx.guild.id)
    if not session:
        return await ctx.send("❌ No hay karaoke activo. Usa `¡karaoke iniciar` primero.")

    if _karaoke_user_entry(session, ctx.author.id):
        return await ctx.send("⚠️ Ya tienes una canción/turno registrado en este karaoke.")

    msg = await ctx.send("🔍 Buscando tu canción para karaoke...")
    try:
        song = await resolve_song(cancion, ctx.author)
        entry = {
            "member_id": ctx.author.id,
            "display_name": ctx.author.display_name,
            "song_title": song["title"],
            "url": song["url"],
            "web_url": song["web_url"],
            "thumbnail": song.get("thumbnail"),
            "duration": song.get("duration", 0),
            "score": None,
            "comment": None
        }
        session.setdefault("participants", set()).add(ctx.author.id)
        session.setdefault("queue", []).append(entry)
        await msg.edit(content=f"✅ {ctx.author.mention} quedó apuntado con **{song['title']}**. Turnos en cola: **{len(session['queue'])}**")
    except Exception as e:
        await msg.edit(content=f"❌ No pude agregar esa canción: {str(e)[:500]}")


@karaoke_group.command(name="cola", aliases=["lista", "queue"])
async def karaoke_cola(ctx: commands.Context):
    session = _karaoke_get_session(ctx.guild.id)
    if not session:
        return await ctx.send("📭 No hay karaoke activo.")

    embed = discord.Embed(title="🎤 Cola de Karaoke", color=0xFF2D95)
    current = session.get("current")
    if current:
        embed.add_field(name="Cantando ahora", value=f"<@{current['member_id']}> — **{current['song_title']}**", inline=False)
    if session.get("queue"):
        lines = [f"{i}. <@{e['member_id']}> — **{e['song_title']}**" for i, e in enumerate(session["queue"], 1)]
        embed.description = "\n".join(lines[:15])
    else:
        embed.description = embed.description or "No hay canciones pendientes."
    await ctx.send(embed=embed)


@karaoke_group.command(name="comenzar", aliases=["go", "empezar"])
async def karaoke_comenzar(ctx: commands.Context):
    session = _karaoke_get_session(ctx.guild.id)
    if not session:
        return await ctx.send("❌ No hay karaoke activo. Usa `¡karaoke iniciar` primero.")
    if not session.get("queue"):
        return await ctx.send("📭 No hay canciones en cola. Usa `¡karaoke entrar canción`.")

    moved = await karaoke_move_participants(ctx, session)
    session["running"] = True
    await ctx.send(f"🎬 Karaoke iniciado. Moví **{moved}** participante(s) al canal del host. Turnos aleatorios activados.")
    await karaoke_start_next(ctx)


@karaoke_group.command(name="puntuar", aliases=["score", "calificar"])
async def karaoke_puntuar(ctx: commands.Context, usuario: discord.Member, puntos: int, *, comentario: Optional[str] = None):
    session = _karaoke_get_session(ctx.guild.id)
    if not session:
        return await ctx.send("❌ No hay karaoke activo.")
    if puntos < 0 or puntos > 100:
        return await ctx.send("❌ La puntuación debe estar entre 0 y 100.")

    entry = _karaoke_user_entry(session, usuario.id)
    if not entry:
        return await ctx.send("❌ Ese usuario no está en el karaoke.")

    entry["score"] = puntos
    entry["comment"] = comentario or await karaoke_ai_comment(usuario.display_name, puntos, winner=puntos >= 90)
    await ctx.send(f"✅ {usuario.mention} recibió **{puntos}/100**. 💬 {entry['comment']}")


@karaoke_group.command(name="saltar", aliases=["skip"])
async def karaoke_saltar(ctx: commands.Context):
    session = _karaoke_get_session(ctx.guild.id)
    if not session or not session.get("running"):
        return await ctx.send("❌ No hay karaoke corriendo.")
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    else:
        await karaoke_start_next(ctx)
    await ctx.send("⏭️ Turno saltado.")


@karaoke_group.command(name="finalizar", aliases=["fin", "end", "terminar"])
async def karaoke_finalizar(ctx: commands.Context):
    session = _karaoke_get_session(ctx.guild.id)
    if not session:
        return await ctx.send("📭 No hay karaoke activo.")
    session["running"] = False
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    await karaoke_show_results(ctx, session)
    KARAOKE_SESSIONS.pop(ctx.guild.id, None)


# --------------------------
# Módulo de Moderación Automática
# --------------------------

# Estructuras para seguimiento de infracciones
user_warnings: Dict[int, Dict[str, int]] = {}  # {user_id: {"strikes": count, "last_warning": timestamp}}
allowed_channels: Dict[int, bool] = {}  # Canales donde se permite lenguaje fuerte
malicious_domains: Set[str] = set()  # Dominios maliciosos conocidos
safe_domains: Set[str] = {
    "discord.com",
    "youtube.com",
    "spotify.com",
    "steampowered.com",
    "store.steampowered.com",
    "steamcommunity.com",
    "workshop.steampowered.com",
    "github.com",
    "docs.google.com",
    "drive.google.com",
    "twitter.com",
    "reddit.com",
    "twitch.tv"
}

# Configuración de moderación
MODERATION_SETTINGS = {
    "max_warnings": 2,
    "timeout_duration": 900,
    "cooldown_period": 3600,
    "toxic_threshold": 0.7,
    "spam_threshold": 5,
    "spam_cooldown": 300
}

def save_moderation_data():
    """Guarda los datos de moderación en un archivo JSON"""
    data = {
        "user_warnings": user_warnings,
        "allowed_channels": {str(k): v for k, v in allowed_channels.items()},
        "malicious_domains": list(malicious_domains)
    }

    try:
        with open(MODERATION_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Datos de moderación guardados correctamente")
    except Exception as e:
        logger.error(f"Error al guardar datos de moderación: {e}")

def load_moderation_data():
    """Carga los datos de moderación desde un archivo JSON"""
    global user_warnings, allowed_channels, malicious_domains

    try:
        if os.path.exists(MODERATION_DATA_PATH):
            with open(MODERATION_DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

                # Convertir keys de allowed_channels de str a int
                allowed_channels = {int(k): v for k, v in data.get("allowed_channels", {}).items()}

                # Convertir user_warnings keys de str a int
                user_warnings = {int(k): v for k, v in data.get("user_warnings", {}).items()}

                malicious_domains.update(set(data.get("malicious_domains", [])))

            logger.info("Datos de moderación cargados correctamente")
    except Exception as e:
        logger.error(f"Error al cargar datos de moderación: {e}")
        # Inicializar estructuras vacías si hay error
        user_warnings = {}
        allowed_channels = {}
        malicious_domains = set()

async def save_data_periodically():
    """Guarda los datos de moderación periódicamente"""
    while True:
        await asyncio.sleep(300)  # Guardar cada 5 minutos
        save_moderation_data()

async def load_malicious_domains():
    """Carga dominios maliciosos conocidos desde una fuente externa"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    "https://raw.githubusercontent.com/nikolaischunk/discord-phishing-links/main/domain-list.json") as resp:
                if resp.status == 200:
                    text = await resp.text()
                    data = json.loads(text)
                    malicious_domains.update(data["domains"])
                    save_moderation_data()
    except Exception as e:
        logger.error(f"Error al cargar dominios maliciosos: {e}")
        # Lista de respaldo por si falla la carga
        malicious_domains.update([
            "discord-gift.com", "steamcommunty.com",
            "nitro-gift.com", "free-nitro.ru"
        ])


def basic_message_analysis(content: str) -> Dict[str, float]:
    """Moderación barata sin IA para no quemar Gemini."""
    text = (content or "").lower()

    threat_words = [
        "te voy a matar", "te mato", "matarte", "doxx", "doxear", "amenaza",
        "voy a hackearte", "te voy a partir"
    ]
    spam_score = 1.0 if len(content) > 700 or len(re.findall(r"(https?://|discord\.gg)", text)) >= 3 else 0.0
    amenazas = 0.95 if any(w in text for w in threat_words) else 0.0

    # No castigamos insultos comunes de confianza; para eso existe el canal/servidor humano.
    toxicidad = 0.0
    acoso = 0.0

    return {
        "toxicidad": toxicidad,
        "acoso": acoso,
        "amenazas": amenazas,
        "spam": spam_score,
        "enlaces_maliciosos": 0.0
    }


async def analyze_message_content(message: discord.Message) -> Dict[str, float]:
    """
    Analiza mensajes sin gastar Gemini por defecto.

    Antes cada mensaje hacía una llamada a Gemini y por eso se agotaba la cuota (429).
    Ahora la IA solo se usa si MODERATION_AI_ENABLED=true y además tiene cooldown si la API se queda sin cuota.
    """
    global gemini_moderation_cooldown_until

    fallback = basic_message_analysis(message.clean_content)

    if not MODERATION_AI_ENABLED or model is None:
        return fallback

    if time.time() < gemini_moderation_cooldown_until:
        return fallback

    try:
        prompt = (
            "Analiza el siguiente mensaje de Discord y responde ÚNICAMENTE con un objeto JSON con puntuaciones entre 0 y 1, así:\n"
            "{\"toxicidad\": 0.0, \"acoso\": 0.0, \"amenazas\": 0.0, \"spam\": 0.0, \"enlaces_maliciosos\": 0.0}\n\n"
            "NO escribas explicaciones, contexto ni texto adicional. Solo devuelve el JSON en una sola línea.\n\n"
            f"Mensaje: '{message.clean_content[:1500]}'\n"
        )

        response = model.generate_content(prompt)
        respuesta_texto = (response.text or "").strip()

        if respuesta_texto.startswith("```"):
            respuesta_texto = re.sub(r"^```[a-zA-Z]*\n?", "", respuesta_texto)
            respuesta_texto = respuesta_texto.rstrip("```").strip()

        if not respuesta_texto.startswith("{"):
            logger.warning(f"Respuesta inválida de la IA en moderación: '{respuesta_texto[:100]}'")
            return fallback

        analysis = json.loads(respuesta_texto)
        return {**fallback, **analysis}

    except Exception as e:
        error_text = str(e).lower()
        if "429" in error_text or "quota" in error_text or "rate" in error_text:
            gemini_moderation_cooldown_until = time.time() + 3600
            logger.warning("Gemini llegó al límite de cuota. Moderación IA pausada 1 hora; sigo con moderación básica.")
        else:
            logger.error(f"Error al analizar mensaje con IA: {e}")
        return fallback


async def check_malicious_links(content: str) -> bool:
    """Verifica enlaces con lista local sin gastar Gemini."""
    urls = URL_REGEX.findall(content or "")
    if not urls:
        return False

    suspicious_keywords = ("nitro", "gift", "airdrop", "free", "steam", "discord")
    suspicious_tlds = (".ru", ".cn", ".tk", ".top", ".xyz", ".click", ".zip")

    for url in urls:
        parsed = urllib.parse.urlparse(url)
        domain = (parsed.netloc or "").lower().strip()
        domain = domain[4:] if domain.startswith("www.") else domain

        if not domain:
            continue

        if domain in safe_domains or any(domain.endswith("." + d) for d in safe_domains):
            continue

        if domain in malicious_domains:
            return True

        # Detección simple de typosquatting frecuente.
        compact = domain.replace("-", "").replace(".", "")
        if "discord" in compact and not domain.endswith("discord.com"):
            return True
        if "steamcommunity" in compact and "steamcommunity.com" not in domain:
            return True

        if any(k in compact for k in suspicious_keywords) and domain.endswith(suspicious_tlds):
            return True

    return False


async def send_warning(user: discord.Member, reason: str, strike_count: int):
    """Envía una advertencia personalizada generada por IA"""
    try:
        prompt = (
            f"Escribe un mensaje de advertencia para {user.display_name} que ha violado las reglas del servidor. "
            f"Razón: {reason}\n"
            f"Este es su strike #{strike_count}. "
            "El tono debe ser profesional pero no demasiado formal. "
            "Explica claramente qué hizo mal y cómo puede evitarlo en el futuro. "
            "Incluye un emoji al inicio para llamar la atención."
        )

        response = model.generate_content(prompt)
        warning_msg = response.text

        try:
            await user.send(warning_msg)
        except discord.Forbidden:
            channel = next((c for c in user.guild.text_channels if c.permissions_for(user.guild.me).send_messages),
                           None)
            if channel:
                await channel.send(f"{user.mention} {warning_msg}")
    except Exception as e:
        logger.error(f"Error al enviar advertencia: {e}")

async def apply_timeout(user: discord.Member, reason: str):
    """Aplica un timeout al usuario con mensaje generado por IA"""
    try:
        prompt = (
            f"Escribe un mensaje para {user.display_name} que ha sido silenciado temporalmente (timeout) en el servidor. "
            f"Razón: {reason}\n"
            "El tono debe ser firme pero constructivo. "
            "Explica la duración (15 minutos) y qué puede hacer para evitar sanciones mayores. "
            "Usa un estilo similar a un moderador humano."
        )

        response = model.generate_content(prompt)
        timeout_msg = response.text

        await user.timeout(timedelta(seconds=MODERATION_SETTINGS["timeout_duration"]), reason=reason)
        save_moderation_data()

        try:
            await user.send(timeout_msg)
        except discord.Forbidden:
            channel = next((c for c in user.guild.text_channels if c.permissions_for(user.guild.me).send_messages),
                           None)
            if channel:
                await channel.send(f"{user.mention} {timeout_msg}")
    except Exception as e:
        logger.error(f"Error al aplicar timeout: {e}")

async def apply_ban(user: discord.Member, reason: str):
    """Aplica un ban al usuario con mensaje generado por IA"""
    try:
        prompt = (
            f"Escribe un mensaje para {user.display_name} que ha sido expulsado permanentemente del servidor. "
            f"Razón: {reason}\n"
            "El tono debe ser definitivo pero profesional. "
            "Explica que esta decisión se tomó después de múltiples advertencias. "
            "Incluye información sobre cómo apelar si el servidor lo permite."
        )

        response = model.generate_content(prompt)
        ban_msg = response.text

        try:
            await user.send(ban_msg)
        except discord.Forbidden:
            pass

        await user.ban(reason=reason, delete_message_days=1)
        save_moderation_data()

        log_channel = discord.utils.get(user.guild.text_channels, name="mod-log")
        if log_channel:
            embed = discord.Embed(
                title="🚨 Usuario baneado",
                description=f"**Usuario:** {user.mention}\n**Razón:** {reason}",
                color=discord.Color.red()
            )
            await log_channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Error al aplicar ban: {e}")

async def check_channel_tolerance(channel: discord.TextChannel) -> bool:
    """Determina si un canal permite lenguaje relajado sin gastar IA por defecto."""
    if channel.id in allowed_channels:
        return allowed_channels[channel.id]

    if not MODERATION_AI_ENABLED or model is None:
        return False

    try:
        messages = [m.clean_content async for m in channel.history(limit=12)]
        sample = "\n".join(messages[:5])[:1200]

        prompt = (
            "Analiza el tono general de este canal de Discord basado en estos mensajes:\n"
            f"{sample}\n\n"
            "Responde solo con 'true' si el canal tiene un ambiente informal donde se permite lenguaje fuerte entre amigos, "
            "o 'false' si es un canal más formal donde ese lenguaje sería inapropiado."
        )

        response = model.generate_content(prompt)
        is_relaxed = response.text.strip().lower() == "true"
        allowed_channels[channel.id] = is_relaxed
        save_moderation_data()
        return is_relaxed
    except Exception as e:
        logger.warning(f"No pude analizar tolerancia de canal; uso estricto básico: {e}")
        return False


async def moderate_message(message: discord.Message):
    """Función principal de moderación de mensajes"""
    if message.id in bypass_messages:
        bypass_messages.discard(message.id)
        return

    if message.author.bot or message.is_system() or not message.content:
        return

    if not malicious_domains:
        await load_malicious_domains()

    has_malicious_links = await check_malicious_links(message.content)
    analysis = await analyze_message_content(message)
    channel_tolerant = await check_channel_tolerance(message.channel)

    toxicity_threshold = MODERATION_SETTINGS["toxic_threshold"] * (0.7 if channel_tolerant else 1.0)

    violations = []
    if has_malicious_links:
        violations.append("enlaces maliciosos")
    if analysis["amenazas"] > 0.8:
        violations.append("amenazas")
    if analysis["acoso"] > 0.85:
        violations.append("acoso")
    if analysis["toxicidad"] > toxicity_threshold and not channel_tolerant:
        violations.append("lenguaje tóxico")
    if analysis["spam"] > 0.9:
        violations.append("spam")

    if not violations:
        return

    user_id = message.author.id
    if user_id not in user_warnings:
        user_warnings[user_id] = {"strikes": 0, "last_warning": 0}

    current_time = time.time()
    last_warning = user_warnings[user_id]["last_warning"]

    if current_time - last_warning < MODERATION_SETTINGS["cooldown_period"]:
        return

    severity = "high" if "enlaces maliciosos" in violations or "amenazas" in violations else "medium" if "acoso" in violations else "low"

    if severity == "high":
        user_warnings[user_id]["strikes"] += 2
    else:
        user_warnings[user_id]["strikes"] += 1

    user_warnings[user_id]["last_warning"] = current_time
    strike_count = user_warnings[user_id]["strikes"]
    save_moderation_data()

    try:
        await message.delete()
    except discord.Forbidden:
        logger.warning(f"No se pudo borrar mensaje de {message.author}")

    reason = ", ".join(violations)

    if strike_count >= 3:
        await apply_ban(message.author, reason)
        del user_warnings[user_id]
    elif strike_count >= 2:
        await apply_timeout(message.author, reason)
    else:
        await send_warning(message.author, reason, strike_count)


# --------------------------
# Módulo de Utilidades
# --------------------------

@bot.command(name='borrarchat')
async def wipe_dm(ctx):
    """Borra todo el historial de este DM (tus mensajes y los del bot)"""
    if not isinstance(ctx.channel, discord.DMChannel):
        return await ctx.send("❌ Este comando solo funciona en mensajes directos", delete_after=10)
    
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)  # 1 minuto de timeout
        
        async def on_timeout(self):
            # Limpiar los botones cuando expire el tiempo
            for item in self.children:
                item.disabled = True
            try:
                await self.message.edit(view=self)
            except:
                pass
        
        @discord.ui.button(label="BORRAR TODO", style=discord.ButtonStyle.red)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            try:
                # Responder inmediatamente a la interacción
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                
                # Borrar mensajes del bot
                deleted_count = 0
                async for msg in interaction.channel.history(limit=None):
                    if msg.author == bot.user and msg.id != ctx.message.id:
                        try:
                            await msg.delete()
                            deleted_count += 1
                            await asyncio.sleep(0.7)
                        except:
                            continue
                
                # Enviar mensaje con botón de auto-borrado
                embed = discord.Embed(
                    title="✅ Borrado completado",
                    description=f"Se borraron {deleted_count} mensajes del bot.\n\n"
                               "**Para borrar TUS mensajes:**\n"
                               "1. En PC: Haz clic derecho > Borrar\n"
                               "2. En móvil: Mantén presionado > Eliminar",
                    color=0x00ff00
                )
                
                class DeleteView(discord.ui.View):
                    @discord.ui.button(label="Borrar este mensaje", style=discord.ButtonStyle.grey, emoji="🗑️")
                    async def delete_callback(self, inner_interaction: discord.Interaction, button: discord.ui.Button):
                        await inner_interaction.message.delete()
                        try:
                            await inner_interaction.response.send_message("✅ Mensaje eliminado", ephemeral=True, delete_after=3)
                        except:
                            pass
                
                try:
                    await interaction.followup.send(embed=embed, view=DeleteView())
                except Exception as e:
                    logger.error(f"Error enviando followup: {e}")
                    await interaction.channel.send(embed=embed, view=DeleteView())
                
            except Exception as e:
                logger.error(f"Error en confirm: {e}")
                try:
                    await interaction.followup.send("❌ Error al procesar la solicitud", ephemeral=True)
                except:
                    await interaction.channel.send("❌ Error al procesar la solicitud")

    embed = discord.Embed(
        title="⚠️ BORRAR HISTORIAL COMPLETO",
        description="Esto eliminará **todos los mensajes del bot** en este chat.\n\n"
                   "¿Estás seguro?",
        color=0xff0000
    )
    
    view = ConfirmView()
    view.message = await ctx.send(embed=embed, view=view)

@bot.command(name="votar")
async def votar(ctx, *args):
    """Crea encuestas con o sin tiempo personalizado.
    Uso 1: ¡votar "¿Pregunta?" op1 op2 (1 minuto por defecto)
    Uso 2: ¡votar 5 "¿Pregunta?" op1 op2 (5 minutos)"""

    # Configuración inicial
    tiempo_minutos = 1  # Valor por defecto
    pregunta = ""
    opciones = []
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣']

    # Procesar argumentos
    try:
        # Caso 1: ¡votar "pregunta" op1 op2
        if not args[0].isdigit():
            pregunta = args[0]
            opciones = list(args[1:])

        # Caso 2: ¡votar 5 "pregunta" op1 op2
        else:
            tiempo_minutos = int(args[0])
            pregunta = args[1]
            opciones = list(args[2:])

        # Validaciones
        if len(opciones) < 2:
            return await ctx.send("❌ Necesitas al menos 2 opciones.")
        if len(opciones) > 6:
            return await ctx.send("⚠️ Máximo 6 opciones permitidas.")
        if tiempo_minutos <= 0:
            return await ctx.send("❌ El tiempo debe ser mayor a 0 minutos.")

    except IndexError:
        return await ctx.send("❌ Formato incorrecto. Ejemplos:\n"
                              "`¡votar \"¿Pregunta?\" op1 op2`\n"
                              "`¡votar 3 \"¿Pregunta?\" op1 op2`")

    # Crear embed
    embed = discord.Embed(
        title=f"📊 {pregunta}",
        description="\n".join([f"{emojis[i]} {op}" for i, op in enumerate(opciones)]),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"⏳ Votación abierta por {tiempo_minutos} minuto(s)")

    # Enviar y añadir reacciones
    mensaje = await ctx.send(embed=embed)
    for i in range(len(opciones)):
        await mensaje.add_reaction(emojis[i])

    # Esperar y calcular resultados
    await asyncio.sleep(tiempo_minutos * 60)
    mensaje_actualizado = await ctx.channel.fetch_message(mensaje.id)

    resultados = {}
    for i, emoji in enumerate(emojis[:len(opciones)]):
        for reaccion in mensaje_actualizado.reactions:
            if str(reaccion.emoji) == emoji:
                resultados[opciones[i]] = reaccion.count - 1

    # Determinar ganador
    if not resultados:
        return await ctx.send("🤷 Nadie votó.")

    ganador = max(resultados.items(), key=lambda x: x[1])
    porcentaje = (ganador[1] / sum(resultados.values())) * 100

    # Generar comentario con IA
    try:
        respuesta = model.generate_content(
            f"Crea un comentario gracioso (1 línea) sobre esta votación: "
            f"'{pregunta}'. Ganador: '{ganador[0]}' con {porcentaje:.1f}% votos."
        )
        comentario = respuesta.text
    except Exception:
        comentario = "¡Y el veredicto es...!"

    # Mostrar resultados
    embed_resultado = discord.Embed(
        title=f"🎉 Ganador: {ganador[0]} ({porcentaje:.1f}%)",
        description=f"**{pregunta}**\n\n{comentario}",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed_resultado)

@bot.command(name='eliminar_strikes', aliases=['reset_strikes', 'borrar_strikes'])
@commands.has_permissions(administrator=True)  # Solo administradores pueden usarlo
async def eliminar_strikes(ctx, usuario: discord.Member = None):
    """Elimina todos los strikes de un usuario (Solo Admin)"""
    if usuario is None:
        return await ctx.send("❌ Debes mencionar a un usuario. Ejemplo: `¡eliminar_strikes @usuario`", delete_after=10)

    user_id = usuario.id

    if user_id not in user_warnings:
        return await ctx.send(f"ℹ️ {usuario.mention} no tiene strikes registrados.", delete_after=10)

    # Eliminar registro
    del user_warnings[user_id]

    # Mensaje de confirmación
    embed = discord.Embed(
        title="✅ Strikes Eliminados",
        description=f"Se han removido todos los strikes de {usuario.mention}",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Moderador: {ctx.author.display_name}")

    await ctx.send(embed=embed)

    # Registrar en logs
    logger.info(f"Strikes eliminados para {usuario.name} (ID: {user_id}) por {ctx.author.name}")

    # Opcional: Enviar DM al usuario
    try:
        await usuario.send(f"♻️ Tus strikes han sido reiniciados por {ctx.author.mention} en {ctx.guild.name}")
    except discord.Forbidden:
        pass

@eliminar_strikes.error
async def eliminar_strikes_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Solo los administradores pueden usar este comando.", delete_after=10)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Usuario no válido. Debes mencionar a alguien.", delete_after=10)
    else:
        logger.error(f"Error en eliminar_strikes: {error}")
        await ctx.send("⚠️ Ocurrió un error al procesar el comando.", delete_after=10)


@bot.command(name='limpiar', aliases=['purge', 'clear'])
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def limpiar(ctx, cantidad: str = "10"):
    """
    Elimina mensajes del chat
    Uso:
    !limpiar 10 - Borra 10 mensajes
    !limpiar all - Borra todo el historial (hasta 1000 mensajes)
    !limpiar user @usuario - Borra mensajes de un usuario específico
    """
    try:
        # Opción para borrar TODO el historial (limitado a 1000 mensajes por seguridad)
        if cantidad.lower() == 'all':
            deleted = await ctx.channel.purge(limit=1000, check=lambda m: not m.pinned)
            msg = await ctx.send(f"🧹 Nuke completo! Eliminados {len(deleted)} mensajes", delete_after=10)
            return

        # Opción para borrar mensajes de un usuario específico
        if cantidad.lower() == 'user' and ctx.message.mentions:
            user = ctx.message.mentions[0]
            deleted = await ctx.channel.purge(
                limit=100,
                check=lambda m: m.author == user and not m.pinned
            )
            msg = await ctx.send(f"🧹 Eliminados {len(deleted)} mensajes de {user.display_name}", delete_after=10)
            return

        # Borrado normal por cantidad
        cantidad_num = int(cantidad)
        if 1 <= cantidad_num <= 1000:  # Aumentado el límite máximo
            deleted = await ctx.channel.purge(
                limit=cantidad_num + 1,  # +1 para incluir el comando
                check=lambda m: not m.pinned  # Ignora mensajes fijados
            )
            msg = await ctx.send(f"🧹 Eliminados {len(deleted) - 1} mensajes", delete_after=5)
        else:
            await ctx.send("❌ Cantidad inválida (1-1000 o 'all')", delete_after=5)

    except ValueError:
        await ctx.send("❌ Formato incorrecto. Usa: `¡limpiar 10`, `¡limpiar all` o `¡limpiar user @usuario`",
                       delete_after=10)
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}", delete_after=10)
        print(f"Error en comando limpiar: {traceback.format_exc()}")


@bot.command(name='silenciar')
@commands.has_permissions(kick_members=True)
async def silenciar(ctx, miembro: discord.Member, *, razón: str = "Sin razón"):
    """Silencia a un usuario"""
    role = discord.utils.get(ctx.guild.roles, name="Silenciado")
    if not role:
        role = await ctx.guild.create_role(name="Silenciado")
        for channel in ctx.guild.channels:
            await channel.set_permissions(role, send_messages=False)

    await miembro.add_roles(role)
    embed = discord.Embed(
        title=f"🔇 {miembro.display_name} silenciado",
        description=f"Razón: {razón}",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)



# --------------------------
# Sistema moderno de tickets
# --------------------------

def is_ticket_request_channel(channel: Optional[discord.abc.GuildChannel]) -> bool:
    """Comprueba si el mensaje viene del canal público donde se solicitan tickets."""
    return bool(channel and getattr(channel, "id", 0) == TICKET_REQUEST_CHANNEL_ID)


def get_ticket_staff_roles(guild: discord.Guild) -> List[discord.Role]:
    """Obtiene roles de staff configurados para tickets."""
    roles: List[discord.Role] = []
    for role_id in TICKET_STAFF_ROLE_IDS:
        role = guild.get_role(role_id)
        if role:
            roles.append(role)
    return roles


async def safe_dm(user: Union[discord.User, discord.Member], *args, **kwargs) -> bool:
    """Intenta enviar DM sin romper el flujo si Discord lo bloquea."""
    try:
        await user.send(*args, **kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.info(f"No pude enviar DM a {getattr(user, 'id', 'desconocido')}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Error inesperado enviando DM: {e}")
        return False


async def get_or_create_ticket_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Crea o reutiliza una categoría privada para tickets."""
    category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
    if category:
        return category

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True
        ),
    }

    for role in get_ticket_staff_roles(guild):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True
        )

    return await guild.create_category(TICKET_CATEGORY_NAME, overwrites=overwrites)


def sanitize_channel_name(name: str) -> str:
    """Convierte un nombre de usuario en nombre seguro de canal."""
    name = re.sub(r"[^a-zA-Z0-9áéíóúüñÁÉÍÓÚÜÑ_-]+", "-", name.lower()).strip("-")
    return name[:40] or "usuario"


def build_ticket_embed(user: discord.Member, motivo: str, source_channel: Optional[discord.TextChannel] = None) -> discord.Embed:
    """Embed principal del ticket."""
    embed = discord.Embed(
        title="🎫 Ticket de soporte",
        description=(
            f"**Usuario:** {user.mention} (`{user.id}`)\n"
            f"**Motivo:** {motivo or 'No especificado'}\n"
            f"**Canal origen:** {source_channel.mention if source_channel else 'Comando slash'}\n"
            f"**Fecha:** {discord.utils.format_dt(discord.utils.utcnow(), style='f')}"
        ),
        color=discord.Color.orange()
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"Ticket creado por {user.display_name}")
    return embed


async def send_ticket_log(guild: discord.Guild, embed: discord.Embed, ticket_channel: Optional[discord.TextChannel] = None):
    """Envía registro al canal de logs sin romper el ticket si no hay permisos."""
    log_channel = guild.get_channel(TICKET_LOG_CHANNEL_ID) or bot.get_channel(TICKET_LOG_CHANNEL_ID)
    if not isinstance(log_channel, discord.TextChannel):
        logger.warning(f"No encontré canal de logs de tickets con ID {TICKET_LOG_CHANNEL_ID}.")
        return

    log_embed = embed.copy()
    if ticket_channel:
        log_embed.add_field(name="Canal privado", value=ticket_channel.mention, inline=False)

    try:
        await log_channel.send(embed=log_embed)
    except Exception as e:
        logger.warning(f"No pude enviar log de ticket: {e}")


class TicketControlView(discord.ui.View):
    """Botones dentro del canal privado del ticket."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id

    def is_staff_or_owner(self, member: discord.Member) -> bool:
        if member.id == self.owner_id:
            return True
        staff_roles = set(TICKET_STAFF_ROLE_IDS)
        return any(role.id in staff_roles for role in getattr(member, "roles", [])) or member.guild_permissions.manage_channels

    @discord.ui.button(label="Cerrar ticket", emoji="🔒", style=discord.ButtonStyle.danger)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not self.is_staff_or_owner(interaction.user):
            return await interaction.response.send_message("❌ Solo el creador o soporte puede cerrar este ticket.", ephemeral=True)

        await interaction.response.send_message("🔒 Cerrando ticket en 5 segundos...", ephemeral=True)

        try:
            close_embed = discord.Embed(
                title="🔒 Ticket cerrado",
                description=f"Cerrado por {interaction.user.mention}\nCanal: `{interaction.channel.name}`",
                color=discord.Color.red()
            )
            await send_ticket_log(interaction.guild, close_embed)
        except Exception:
            pass

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket cerrado por {interaction.user}")
        except Exception as e:
            logger.warning(f"No pude borrar canal de ticket: {e}")


async def create_private_ticket(
    guild: discord.Guild,
    user: discord.Member,
    motivo: str,
    source_channel: Optional[discord.TextChannel] = None,
) -> Optional[discord.TextChannel]:
    """Crea un canal privado donde solo ve el avance el usuario y el staff."""
    now = time.time()
    last = ticket_cooldowns.get(user.id, 0)
    if now - last < 10:
        return None
    ticket_cooldowns[user.id] = now

    category = await get_or_create_ticket_category(guild)
    staff_roles = get_ticket_staff_roles(guild)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True
        ),
        user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True
        ),
    }

    for role in staff_roles:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True
        )

    channel_name = f"ticket-{sanitize_channel_name(user.display_name)}-{user.discriminator if getattr(user, 'discriminator', '0') != '0' else str(user.id)[-4:]}"
    ticket_channel = await guild.create_text_channel(
        name=channel_name[:90],
        category=category,
        overwrites=overwrites,
        topic=f"Ticket de {user} | ID usuario: {user.id} | Motivo: {motivo[:200]}",
        reason=f"Ticket creado por {user}"
    )

    ticket_embed = build_ticket_embed(user, motivo, source_channel)
    staff_mentions = " ".join(role.mention for role in staff_roles) if staff_roles else "`Sin rol staff configurado`"

    intro = (
        f"{user.mention} {staff_mentions}\n\n"
        "✅ **Ticket creado.** Aquí puedes explicar tu problema con capturas, detalles y paciencia gamer.\n"
        "Solo tú, soporte y el bot pueden ver este canal."
    )

    await ticket_channel.send(content=intro, embed=ticket_embed, view=TicketControlView(user.id))
    await send_ticket_log(guild, ticket_embed, ticket_channel)

    await safe_dm(
        user,
        embed=discord.Embed(
            title="✅ Ticket creado",
            description=f"Tu ticket fue creado en {ticket_channel.mention}\n**Motivo:** {motivo}",
            color=discord.Color.green()
        )
    )

    return ticket_channel


class TicketReasonModal(discord.ui.Modal, title="Crear ticket"):
    motivo = discord.ui.TextInput(
        label="¿Qué necesitas?",
        style=discord.TextStyle.long,
        max_length=800,
        placeholder="Explica tu problema. Ejemplo: necesito ayuda con roles, reportar un bug, soporte del server...",
        required=True
    )

    def __init__(self):
        super().__init__()

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                return await interaction.response.send_message("❌ Esto solo funciona dentro del servidor.", ephemeral=True)

            channel = await create_private_ticket(
                interaction.guild,
                interaction.user,
                str(self.motivo.value),
                interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
            )

            if channel is None:
                return await interaction.response.send_message(
                    "⏳ Espera unos segundos antes de abrir otro ticket.",
                    ephemeral=True
                )

            await interaction.response.send_message(
                f"✅ Ticket creado: {channel.mention}\nSolo tú y soporte pueden ver el avance.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error creando ticket desde modal: {traceback.format_exc()}")
            try:
                await interaction.response.send_message("❌ No pude crear el ticket. Revisa permisos del bot.", ephemeral=True)
            except Exception:
                pass


class TicketOpenView(discord.ui.View):
    """Panel público para abrir tickets con botón."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir ticket", emoji="🎫", style=discord.ButtonStyle.success, custom_id="archeon_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketReasonModal())


def build_ticket_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🎫 Centro de soporte",
        description=(
            "¿Necesitas ayuda? Pulsa el botón de abajo y se creará un canal privado.\n\n"
            "🔒 **Privado:** solo tú y soporte verán el avance.\n"
            "🧹 **Limpio:** este canal no es para chatear; los mensajes sueltos se borran.\n"
            "📌 **Consejo:** explica el problema con detalles para resolverlo más rápido."
        ),
        color=0xF59E0B
    )
    embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else discord.Embed.Empty)
    embed.set_footer(text="Sistema de Tickets • Archeon")
    return embed


async def ensure_ticket_panel(guild: discord.Guild):
    """Publica un panel bonito en el canal de tickets y limpia paneles viejos del bot."""
    if not TICKET_PANEL_ENABLED:
        return

    channel = guild.get_channel(TICKET_REQUEST_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        logger.warning(f"No encontré canal público de tickets con ID {TICKET_REQUEST_CHANNEL_ID}.")
        return

    try:
        me = guild.me
        perms = channel.permissions_for(me)
        if perms.manage_messages:
            async for msg in channel.history(limit=30):
                if msg.author.id == bot.user.id and msg.embeds and "Centro de soporte" in (msg.embeds[0].title or ""):
                    try:
                        await msg.delete()
                    except Exception:
                        pass

        if perms.send_messages:
            await channel.send(embed=build_ticket_panel_embed(guild), view=TicketOpenView())
    except Exception as e:
        logger.warning(f"No pude publicar panel de tickets: {e}")


@bot.command(name='ticket')
async def crear_ticket(ctx: commands.Context, *, motivo: str = "Sin motivo especificado"):
    """Crea un canal privado de soporte. Uso: ¡ticket necesito ayuda con..."""
    try:
        try:
            await ctx.message.delete()
        except Exception:
            pass

        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return

        ticket_channel = await create_private_ticket(ctx.guild, ctx.author, motivo, ctx.channel)

        if ticket_channel is None:
            return await ctx.send(
                f"{ctx.author.mention} ⏳ espera unos segundos antes de abrir otro ticket.",
                delete_after=8
            )

        await ctx.send(
            f"{ctx.author.mention} ✅ ticket creado: {ticket_channel.mention}",
            delete_after=10
        )

    except Exception:
        logger.error(f"Error en ticket por prefijo: {traceback.format_exc()}")
        try:
            await ctx.send("❌ No pude crear el ticket. Revisa permisos del bot.", delete_after=10)
        except Exception:
            pass


@bot.command(name="ticket_panel", aliases=["panel_tickets", "setup_tickets"])
@commands.has_permissions(manage_channels=True)
async def ticket_panel_cmd(ctx: commands.Context):
    """Publica el panel de tickets en el canal configurado."""
    await ensure_ticket_panel(ctx.guild)
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send("✅ Panel de tickets actualizado.", delete_after=8)



@bot.event
async def on_raw_reaction_add(payload):
    # Verificar que es el emoji 🔒 en un DM
    if str(payload.emoji) == '🔒' and payload.guild_id is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)

            # Verificar que es un mensaje de ticket y que lo reaccionaste tú
            if message.embeds and "🚨 TICKET CONFIDENCIAL" in message.embeds[0].title:
                if payload.user_id == 607681770422534144:  # Tu ID
                    embed = message.embeds[0]

                    # Extraer ID del usuario - MÉTODO MEJORADO
                    description = embed.description
                    user_match = re.search(r'<@(\d+)>', description)  # Busca el ID entre <@ y >

                    if user_match:
                        user_id = int(user_match.group(1))

                        # Notificar al usuario
                        try:
                            user = await bot.fetch_user(user_id)
                            await user.send(
                                "🔔 **Notificación de soporte**\n"
                                "Hemos recibido tu ticket y lo estamos revisando.\n"
                                "Gracias por tu paciencia."
                            )
                        except Exception as user_error:
                            print(f"No se pudo notificar al usuario {user_id}: {user_error}")
                    else:
                        print("No se encontró ID de usuario en el embed")
        except Exception as e:
            print(f"Error en reacción de ticket: {traceback.format_exc()}")


# ------------------------------------------------------------------
# Comandos de ayuda con botones interactivos
# ------------------------------------------------------------------
class HelpView(discord.ui.View):
    def __init__(self, embeds, ctx):
        super().__init__(timeout=60)
        self.embeds = embeds
        self.current_page = 0
        self.ctx = ctx
        self.message = None

        # Actualizar estado de botones
        self.update_buttons()

    def update_buttons(self):
        # Deshabilitar botones según la página actual
        self.first_page.disabled = self.current_page == 0
        self.prev_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page == len(self.embeds) - 1
        self.last_page.disabled = self.current_page == len(self.embeds) - 1

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, disabled=True)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await self.update_page(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.primary, disabled=True)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        await self.update_page(interaction)

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger)
    async def close_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.message.delete()
        self.stop()

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        await self.update_page(interaction)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = len(self.embeds) - 1
        await self.update_page(interaction)

    async def update_page(self, interaction: discord.Interaction):
        self.update_buttons()
        embed = self.embeds[self.current_page]
        embed.set_footer(text=f"Página {self.current_page + 1}/{len(self.embeds)} • Usa los botones para navegar")
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.NotFound:
            pass

@bot.command(name="ayuda")
async def mostrar_ayuda(ctx):
    """Muestra un menú interactivo de ayuda con todos los comandos"""
    prefix = "¡"

    # Crear los embeds para cada categoría
    embeds = []

    # Página 1: Música
    embed1 = discord.Embed(
        title="🎵 Comandos de Música",
        description="Todos los comandos para controlar la reproducción:",
        color=0x1DB954
    )
    embed1.add_field(
        name="Reproducción",
        value=(
            f"`{prefix}reproducir`, `{prefix}play`, `{prefix}p` - Reproduce música\n"
            f"`{prefix}pausar`, `{prefix}pause` - Pausa la música\n"
            f"`{prefix}continuar`, `{prefix}resume` - Reanuda\n"
            f"`{prefix}saltar`, `{prefix}skip`, `{prefix}s` - Salta la canción\n"
            f"`{prefix}detener`, `{prefix}stop` - Detiene todo"
        ),
        inline=False
    )
    embed1.add_field(
        name="Gestión de Cola",
        value=(
            f"`{prefix}cola`, `{prefix}queue` - Muestra la cola\n"
            f"`{prefix}mezclar`, `{prefix}shuffle` - Mezcla la cola\n"
            f"`{prefix}eliminar [pos]`, `{prefix}remove` - Elimina canción\n"
            f"`{prefix}volumen [0-200]`, `{prefix}volume` - Ajusta volumen\n"
            f"`{prefix}reproducir_primero` - Añade al inicio\n"
            f"`{prefix}dj [tema]`, `{prefix}radio` - Modo DJ automático"
        ),
        inline=False
    )
    embed1.set_footer(text=f"Página 1/6 • Prefijo: ¡ • Slash: /")
    embeds.append(embed1)

    # Página 2: Playlists
    embed2 = discord.Embed(
        title="📚 Playlists & Historial",
        description="Gestiona tus listas de reproducción:",
        color=0x9147FF
    )
    embed2.add_field(
        name="Playlists",
        value=(
            f"`{prefix}guardar_playlist [nombre]` - Guarda la cola actual\n"
            f"`{prefix}cargar_playlist [nombre]` - Carga una playlist\n"
            f"`{prefix}listar_playlists` - Muestra tus playlists\n"
            f"`{prefix}historial [fecha]` - Tu historial musical"
        ),
        inline=False
    )
    embed2.set_footer(text=f"Página 2/6 • Prefijo: ¡ • Slash: /")
    embeds.append(embed2)

    # Página 3: IA
    embed3 = discord.Embed(
        title="🧠 Inteligencia Artificial",
        description="Interactúa con nuestra IA:",
        color=0x00B0F4
    )
    embed3.add_field(
        name="Chat",
        value=(
            f"`{prefix}charla [mensaje]` - Conversa con la IA\n"
            f"`{prefix}olvidar` - Reinicia la conversación\n"
            f"`{prefix}imagen [descripción]` - Genera imágenes con IA"
        ),
        inline=False
    )
    embed3.set_footer(text=f"Página 3/6 • Prefijo: ¡ • Slash: /")
    embeds.append(embed3)

    # Página 4: Juegos
    embed4 = discord.Embed(
        title="🎮 Comandos de Juegos",
        description="Diversión para tu servidor:",
        color=0xFFD700
    )
    embed4.add_field(
        name="Organización",
        value=(
            f"`{prefix}separar` - Divide jugadores por juegos\n"
            f"`{prefix}equipos [n]` - Crea equipos aleatorios\n"
            f"`{prefix}karaoke` - Modo karaoke con cola, turnos y ranking"
        ),
        inline=False
    )
    embed4.set_footer(text=f"Página 4/6 • Prefijo: ¡ • Slash: /")
    embeds.append(embed4)

    # Página 5: Utilidades
    embed5 = discord.Embed(
        title="🔧 Utilidades",
        description="Herramientas útiles:",
        color=0x2ECC71
    )
    embed5.add_field(
        name="Encuestas",
        value=(
            f"`{prefix}votar [tiempo] \"pregunta\" op1 op2`\n"
            "Ejemplo: `¡votar 5 \"¿Pizza?\" Sí No`\n"
            "📌 Máx. 6 opciones | ⏱ 1 minuto por defecto"
        ),
        inline=False
    )
    embed5.add_field(
        name="Tickets",
        value=f"`{prefix}ticket [motivo]` - Crea un ticket de soporte",
        inline=False
    )
    embed5.add_field(
        name="Privacidad",
        value=f"`{prefix}borrarchat` - Borra mensajes en DM con el bot",
        inline=False
    )
    embed5.set_footer(text=f"Página 5/6 • Prefijo: ¡ • Slash: /")
    embeds.append(embed5)

    # Página 6: Diversión
    embed6 = discord.Embed(
        title="😂 Comandos Divertidos",
        description="Para pasar un buen rato:",
        color=0xE91E63
    )
    embed6.add_field(
        name="Interacción",
        value=(
            f"`{prefix}insultar @usuario [razón]` - Insulto/roast pesado de broma\n"
            f"`{prefix}abrazo @usuario` - Manda un abrazo virtual\n"
            f"`{prefix}meme` - Genera un meme en español\n"
            f"`{prefix}chiste` - Chiste rápido\n"
            f"`{prefix}moneda` - Cara o cruz\n"
            f"`{prefix}dado [caras]` - Lanza un dado\n"
            f"`{prefix}bola8 [pregunta]` - Bola 8\n"
            f"`{prefix}elegir op1, op2` - Elige por ti\n"
            f"`{prefix}ship @u1 @u2` - Mira qué tanto se quieren esos dos 😳"
        ),
        inline=False
    )
    embed6.set_footer(text=f"Página 6/6 • Prefijo: ¡ • Slash: /")
    embeds.append(embed6)

    # Crear y enviar la vista con botones
    view = HelpView(embeds, ctx)
    view.message = await ctx.send(embed=embeds[0], view=view)

# ----------------------------------------------------
# Comandos de barra (/)
# ----------------------------------------------------
@bot.tree.command(name="ayuda", description="Muestra todos los comandos disponibles del bot")
async def ayuda_slash(interaction: discord.Interaction):
    """Muestra un menú interactivo de ayuda con botones"""
    prefix = "/"

    # Crear los embeds para cada categoría
    embeds = []

    # Página 1: Música
    embed1 = discord.Embed(
        title="🎵 Comandos de Música",
        description="Todos los comandos para controlar la reproducción:",
        color=0x1DB954
    )
    embed1.add_field(
        name="Reproducción",
        value=(
            f"`/reproducir [busqueda]`, `/play [busqueda]` - Reproduce música\n"
            f"`/pausar` - Pausa la música\n"
            f"`/continuar` - Reanuda\n"
            f"`/saltar` - Salta la canción\n"
            f"`/detener` - Detiene todo"
        ),
        inline=False
    )
    embed1.add_field(
        name="Gestión de Cola",
        value=(
            f"`/cola` - Muestra la cola\n"
            f"`/mezclar` - Mezcla la cola\n"
            f"`/eliminar [posición]` - Elimina canción\n"
            f"`/volumen [0-200]` - Ajusta volumen\n"
            f"`/reproducir_primero [busqueda]` - Añade al inicio\n"
            f"`/dj [tema]` - Modo DJ automático\n"\
            f"`/unirse` - Entra a tu canal de voz\n"\
            f"`/mantener_voz on/off` - No me bajo solo"
        ),
        inline=False
    )
    embed1.set_footer(text="Página 1/6 • Usa los botones para navegar")
    embeds.append(embed1)

    # Página 2: Playlists
    embed2 = discord.Embed(
        title="📚 Playlists & Historial",
        description="Gestiona tus listas de reproducción:",
        color=0x9147FF
    )
    embed2.add_field(
        name="Playlists",
        value=(
            f"`/guardar_playlist [nombre]` - Guarda la cola actual\n"
            f"`/cargar_playlist [nombre]` - Carga una playlist\n"
            f"`/listar_playlists` - Muestra tus playlists\n"
            f"`/historial [fecha]` - Tu historial musical"
        ),
        inline=False
    )
    embed2.set_footer(text="Página 2/6 • Usa los botones para navegar")
    embeds.append(embed2)

    # Página 3: IA
    embed3 = discord.Embed(
        title="🧠 Inteligencia Artificial",
        description="Interactúa con nuestra IA:",
        color=0x00B0F4
    )
    embed3.add_field(
        name="Chat",
        value=(
            f"`/charla [mensaje]` - Conversa con la IA\n"
            f"`/olvidar` - Reinicia la conversación\n"
            f"`/imagen [descripción]` - Genera imágenes con IA"
        ),
        inline=False
    )
    embed3.set_footer(text="Página 3/6 • Usa los botones para navegar")
    embeds.append(embed3)

    # Página 4: Juegos
    embed4 = discord.Embed(
        title="🎮 Comandos de Juegos",
        description="Diversión para tu servidor:",
        color=0xFFD700
    )
    embed4.add_field(
        name="Organización",
        value=(
            f"`/separar` - Divide jugadores por juegos\n"
            f"`/equipos [número]` - Crea equipos aleatorios"
        ),
        inline=False
    )
    embed4.set_footer(text="Página 4/6 • Usa los botones para navegar")
    embeds.append(embed4)

    # Página 5: Utilidades
    embed5 = discord.Embed(
        title="🔧 Utilidades",
        description="Herramientas útiles:",
        color=0x2ECC71
    )
    embed5.add_field(
        name="Encuestas",
        value=(
            f"`/votar [pregunta] op1 op2` - Crea una encuesta\n"
            "📌 Máx. 6 opciones | ⏱ 1 minuto por defecto"
        ),
        inline=False
    )
    embed5.add_field(
        name="Tickets",
        value=f"`/ticket [motivo]` - Crea un ticket de soporte\n"\
              f"`/borrarchat` - Borra mensajes del bot en DM",
        inline=False
    )
    embed5.set_footer(text="Página 5/6 • Usa los botones para navegar")
    embeds.append(embed5)

    # Página 6: Diversión
    embed6 = discord.Embed(
        title="😂 Comandos Divertidos",
        description="Para pasar un buen rato:",
        color=0xE91E63
    )
    embed6.add_field(
        name="Interacción",
        value=(
            f"`/insultar @usuario [razón]` - Insulto/roast pesado de broma\n"
            f"`/abrazo @usuario` - Manda un abrazo virtual\n"
            f"`/meme` - Genera un meme en español\n"
            f"`/chiste` - Chiste rápido\n"
            f"`/moneda` - Cara o cruz\n"
            f"`/dado [caras]` - Lanza un dado\n"
            f"`/bola8 [pregunta]` - Bola 8\n"
            f"`/elegir op1, op2` - Elige por ti\n"
            f"`/ship @u1 @u2` - Mira qué tanto se quieren esos dos 😳"
        ),
        inline=False
    )
    embed6.set_footer(text="Página 6/6 • Usa los botones para navegar")
    embeds.append(embed6)

    # Crear y enviar la vista con botones
    view = HelpView(embeds, interaction)
    await interaction.response.send_message(embed=embeds[0], view=view)
    view.message = await interaction.original_response()

# Comando de reproducción mejorado
@bot.tree.command(name="reproducir", description="Reproduce música desde YouTube, Spotify o términos de búsqueda")
@app_commands.describe(busqueda="URL o nombre de la canción")
async def play_slash(interaction: discord.Interaction, busqueda: str):
    """Reproduce música o la añade a la cola (versión slash command mejorada)."""
    try:
        if not interaction.user.voice:
            embed = discord.Embed(
                title="❌ Error de Voz",
                description="Debes estar en un canal de voz para usar este comando.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        embed_cargando = discord.Embed(
            title="🔍 Buscando tu música...",
            description="Estamos procesando tu solicitud, por favor espera.\n\n🎧 *Preparando la mejor calidad de audio para ti*",
            color=discord.Color.orange()
        )
        embed_cargando.set_thumbnail(url="https://i.gifer.com/origin/b4/b4d657e7ef262b88eb5f7ac021edda87.gif")

        await interaction.response.send_message(embed=embed_cargando)
        cargando_msg = await interaction.original_response()

        voice_client = interaction.guild.voice_client
        if not voice_client:
            voice_client = await safe_connect(interaction.user.voice.channel)

        song = await resolve_song(busqueda, interaction.user)

        if voice_client.is_playing() or voice_client.is_paused():
            await add_song_to_queue(interaction.guild.id, song)

            embed = discord.Embed(
                title="🎶 Añadido a la Cola",
                description=f"**[{song['title']}]({song['web_url']})**",
                color=discord.Color.green()
            )
            embed.add_field(name="Posición en cola", value=str(len(queues.get(interaction.guild.id, []))))
            embed.add_field(
                name="Duración",
                value=f"{song['duration'] // 60}:{song['duration'] % 60:02d}" if song.get("duration") else "Desconocida"
            )
            embed.add_field(name="Canal", value=song.get("uploader", "Desconocido"))
            if song.get("thumbnail"):
                embed.set_thumbnail(url=song["thumbnail"])
            embed.set_footer(
                text=f"Solicitado por {interaction.user.display_name}",
                icon_url=interaction.user.display_avatar.url
            )
            return await cargando_msg.edit(embed=embed)

        current_songs[interaction.guild.id] = song
        save_to_history(interaction.guild.id, song)

        source = await create_audio_source(song["url"], interaction.guild.id)

        voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(
                check_queue(interaction),
                bot.loop
            ) if e is None else logger.error(f'Error en reproducción: {e}')
        )

        embed = discord.Embed(
            title="🎵 Reproduciendo Ahora",
            description=f"**[{song['title']}]({song['web_url']})**",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="Duración",
            value=f"{song['duration'] // 60}:{song['duration'] % 60:02d}" if song.get("duration") else "Desconocida"
        )
        embed.add_field(name="Canal", value=song.get("uploader", "Desconocido"))
        if song.get("thumbnail"):
            embed.set_thumbnail(url=song["thumbnail"])
        embed.set_footer(
            text=f"Solicitado por {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url
        )

        await cargando_msg.edit(embed=embed)

    except Exception as e:
        logger.error(f"Error en play_slash: {traceback.format_exc()}")
        msg = str(e)
        if "NoneType" in msg:
            msg = "El extractor devolvió un resultado vacío. Prueba con el nombre exacto o pega el enlace."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Error al reproducir: {msg}"[:2000], ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Error al reproducir: {msg}"[:2000], ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name='saltar', description="Salta la canción actual")
async def skip_slash(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if not voice or not voice.is_playing():
        await interaction.response.send_message("⚠️ No hay música reproduciéndose.", ephemeral=True)
        return

    voice.stop()
    await interaction.response.send_message("⏭️ Canción saltada")

@bot.tree.command(name='pausar', description="Pausa la reproducción actual")
async def pause_slash(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_playing():
        voice.pause()
        await interaction.response.send_message("⏸️ Música pausada")
    else:
        await interaction.response.send_message("⚠️ No hay música reproduciéndose", ephemeral=True)

@bot.tree.command(name='continuar', description="Reanuda la reproducción pausada")
async def resume_slash(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_paused():
        voice.resume()
        await interaction.response.send_message("▶️ Música reanudada")
    else:
        await interaction.response.send_message("⚠️ La música no está pausada", ephemeral=True)

@bot.tree.command(name="cola", description="Muestra la cola de reproducción actual")
async def queue_slash(ctx: discord.Interaction):
    guild_id = ctx.guild.id
    current_song = current_songs.get(guild_id)
    if not queues.get(guild_id) and not current_song:
        await ctx.response.send_message("📭 La cola está vacía", ephemeral=True)
    else:
        embed = discord.Embed(title="🎶 Cola de reproducción", color=discord.Color.purple())

        if current_song:
            duration = ""
            if current_song.get('duration', 0) > 0:
                mins, secs = divmod(current_song['duration'], 60)
                duration = f" [{mins}:{secs:02d}]"

            requested_by = current_song.get('requested_by')
            requester = requested_by.mention if hasattr(requested_by, 'mention') else 'Desconocido'
            embed.add_field(
                name="🔊 Reproduciendo ahora",
                value=f"**{current_song['title']}**{duration}\nSolicitado por: {requester}",
                inline=False
            )

        if queues.get(guild_id):
            for i, item in enumerate(queues[guild_id][:10]):
                duration = ""
                if item.get('duration', 0) > 0:
                    mins, secs = divmod(item['duration'], 60)
                    duration = f" [{mins}:{secs:02d}]"

                requested_by = item.get('requested_by')
                requester = requested_by.display_name if hasattr(requested_by, 'display_name') else 'Desconocido'
                embed.add_field(
                    name=f"{i + 1}. {item['title']}{duration}",
                    value=f"Solicitado por: {requester}",
                    inline=False
                )

            if len(queues[guild_id]) > 10:
                embed.set_footer(text=f"Y {len(queues[guild_id]) - 10} canciones más en la cola...")

        await ctx.response.send_message(embed=embed)


@bot.tree.command(name="desconectar", description="Desconecta el bot del canal de voz")
async def disconnect_command(ctx: discord.Interaction):
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()
        await ctx.response.send_message("Desconectado del canal de voz")
    else:
        await ctx.response.send_message("No estoy conectado a ningún canal de voz", ephemeral=True)


@bot.tree.command(name="mezclar", description="Mezcla aleatoriamente la cola de música")
async def shuffle_slash(ctx: discord.Interaction):
    if ctx.guild.id not in queues or len(queues[ctx.guild.id]) < 2:
        return await ctx.response.send_message("🔀 Necesitas al menos 2 canciones en la cola para mezclar.",
                                               ephemeral=True)

    random.shuffle(queues[ctx.guild.id])
    await ctx.response.send_message("🔀 Cola mezclada aleatoriamente.")


@bot.tree.command(name="eliminar", description="Elimina una canción de la cola por posición")
async def remove_slash(ctx: discord.Interaction, index: int):
    if ctx.guild.id not in queues or index < 1 or index > len(queues[ctx.guild.id]):
        return await ctx.response.send_message("❌ Índice inválido o cola vacía.", ephemeral=True)

    removed = queues[ctx.guild.id].pop(index - 1)
    await ctx.response.send_message(f"🗑️ Canción **{removed['title']}** eliminada de la cola.")


@bot.tree.command(name="volumen", description="Consulta o cambia el volumen de la música")
@app_commands.describe(vol="Nivel de volumen (0-200)")
async def volume_slash(ctx: discord.Interaction, vol: int = None):
    guild_id = ctx.guild.id

    if vol is None:
        current_vol = int(get_guild_volume(guild_id) * 100)
        if ctx.guild.voice_client and ctx.guild.voice_client.source and hasattr(ctx.guild.voice_client.source, 'volume'):
            current_vol = int(ctx.guild.voice_client.source.volume * 100)
        return await ctx.response.send_message(f"🔊 Volumen actual: **{current_vol}%**")

    if vol < 0 or vol > 200:
        return await ctx.response.send_message("❌ El volumen debe estar entre 0 y 200%.", ephemeral=True)

    guild_volumes[guild_id] = vol / 100

    if ctx.guild.voice_client and ctx.guild.voice_client.source and hasattr(ctx.guild.voice_client.source, 'volume'):
        ctx.guild.voice_client.source.volume = vol / 100

    await ctx.response.send_message(f"🔊 Volumen ajustado a **{vol}%**")


@bot.tree.command(name="limpiar_cola", description="Limpia toda la cola de reproducción")
async def clear_slash(ctx: discord.Interaction):
    if ctx.guild.id in queues and queues[ctx.guild.id]:
        queues[ctx.guild.id].clear()
        await ctx.response.send_message("🗑️ Cola de reproducción borrada.")
    else:
        await ctx.response.send_message("📭 La cola ya está vacía.", ephemeral=True)


@bot.tree.command(name="detener", description="Detiene la música y limpia la cola")
async def stop_slash(ctx: discord.Interaction):
    """Detiene la música y limpia la cola"""
    voice = ctx.guild.voice_client

    if not voice or not voice.is_playing():
        return await ctx.response.send_message("⚠️ No hay música reproduciéndose", ephemeral=True)

    # Detener la sesión DJ si está activa
    if ctx.guild.id in dj_sessions:
        dj_sessions[ctx.guild.id]["active"] = False

    if ctx.guild.id in queues:
        queues[ctx.guild.id].clear()

    voice.stop()

    current_songs.pop(ctx.guild.id, None)

    await ctx.response.send_message("⏹️ Música detenida y cola limpiada")

@bot.tree.command(name="reproducir_primero", description="Añade una canción al inicio de la cola")
@app_commands.describe(busqueda="URL o término de búsqueda")
async def playtop_slash(ctx: discord.Interaction, *, busqueda: str):
    if not ctx.user.voice:
        return await ctx.response.send_message("❌ ¡No estás en un canal de voz!", ephemeral=True)

    try:
        voice_client = ctx.guild.voice_client or await safe_connect(ctx.user.voice.channel)
        song = await resolve_song(busqueda, ctx.user)
        await add_song_to_queue(ctx.guild.id, song, front=True)

        if not voice_client.is_playing() and not voice_client.is_paused():
            await check_queue(ctx)
            return await ctx.response.send_message(f"▶️ Reproduciendo desde el inicio: **{song['title']}**")

        await ctx.response.send_message(f"⏫ Canción añadida al inicio de la cola: **{song['title']}**")

    except Exception as e:
        logger.error(f"Error en playtop_slash: {traceback.format_exc()}")
        await ctx.response.send_message(f"❌ Error: {str(e)[:200]}", ephemeral=True)


@bot.tree.command(name="guardar_playlist", description="Guarda la cola actual como playlist")
@app_commands.describe(nombre="Nombre de la playlist")
async def save_slash(ctx: discord.Interaction, nombre: str):
    if not queues.get(ctx.guild.id):
        return await ctx.response.send_message("❌ No hay canciones en la cola para guardar.", ephemeral=True)

    if ctx.guild.id not in saved_playlists:
        saved_playlists[ctx.guild.id] = {}

    saved_playlists[ctx.guild.id][nombre] = queues[ctx.guild.id].copy()
    await ctx.response.send_message(f"💾 Playlist guardada como **{nombre}**.")


@bot.tree.command(name="cargar_playlist", description="Carga una playlist guardada a la cola")
@app_commands.describe(nombre="Nombre de la playlist")
async def load_playlist_slash(ctx: discord.Interaction, nombre: str):
    if ctx.guild.id not in saved_playlists or nombre not in saved_playlists[ctx.guild.id]:
        return await ctx.response.send_message(f"❌ No existe la playlist '{nombre}'", ephemeral=True)

    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = []

    queues[ctx.guild.id].extend(saved_playlists[ctx.guild.id][nombre])
    await ctx.response.send_message(
        f"🎵 Playlist '{nombre}' cargada ({len(saved_playlists[ctx.guild.id][nombre])} canciones)")


@bot.tree.command(name="listar_playlists", description="Muestra tus playlists guardadas")
async def list_playlists_slash(ctx: discord.Interaction):
    if ctx.guild.id not in saved_playlists or not saved_playlists[ctx.guild.id]:
        return await ctx.response.send_message("📭 No hay playlists guardadas", ephemeral=True)

    embed = discord.Embed(title="📋 Playlists Guardadas", color=discord.Color.blue())
    for nombre, canciones in saved_playlists[ctx.guild.id].items():
        embed.add_field(name=nombre, value=f"{len(canciones)} canciones", inline=False)

    await ctx.response.send_message(embed=embed)



@bot.tree.command(name="ticket", description="Crea un ticket privado de soporte")
@app_commands.describe(motivo="Motivo del ticket")
async def ticket_slash(interaction: discord.Interaction, motivo: str = "No especificado"):
    """Crea un canal privado de soporte y responde solo al usuario."""
    try:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Este comando solo funciona dentro del servidor.", ephemeral=True)

        ticket_channel = await create_private_ticket(
            interaction.guild,
            interaction.user,
            motivo,
            interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        )

        if ticket_channel is None:
            return await interaction.response.send_message(
                "⏳ Espera unos segundos antes de abrir otro ticket.",
                ephemeral=True
            )

        await interaction.response.send_message(
            f"✅ Ticket creado: {ticket_channel.mention}\nSolo tú y soporte pueden ver el avance.",
            ephemeral=True
        )

    except Exception:
        logger.error(f"Error en /ticket: {traceback.format_exc()}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ Error al crear el ticket. Revisa permisos del bot.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Error al crear el ticket. Revisa permisos del bot.", ephemeral=True)


@bot.tree.command(name="ticket_panel", description="Publica o actualiza el panel de tickets")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_panel_slash(interaction: discord.Interaction):
    try:
        await ensure_ticket_panel(interaction.guild)
        await interaction.response.send_message("✅ Panel de tickets actualizado.", ephemeral=True)
    except Exception:
        logger.error(f"Error actualizando panel de tickets: {traceback.format_exc()}")
        await interaction.response.send_message("❌ No pude actualizar el panel.", ephemeral=True)


@bot.tree.command(name="dj", description="Modo DJ profesional con reproducción automática")
@app_commands.describe(
    query="Canción, artista o género (deja vacío para automático)",
    theme="Estilo musical (party, chill, workout, focus)"
)
async def dj_slash(interaction: discord.Interaction, query: str = None, theme: str = None):
    """🎧 Modo DJ interactivo con sugerencias inteligentes de música"""
    try:
        # Verificar permisos del canal de voz
        if not interaction.user.voice:
            embed = discord.Embed(
                title="❌ Error de Voz",
                description="Debes estar en un canal de voz para usar este comando!",
                color=0xE74C3C
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # Enviar embed de carga con el GIF profesional
        loading_embed = discord.Embed(
            title="🎧 Iniciando Modo DJ...",
            description="**Preparando experiencia musical premium**\n\n"
                        "⌛ Analizando tus preferencias musicales...",
            color=0x9147FF
        )
        loading_embed.set_thumbnail(url="https://i.pinimg.com/originals/7b/1c/c2/7b1cc273a5db206f9e3e4f4d45c84f07.gif")
        loading_embed.set_footer(text="Sistema DJ Archeon • Procesando tu solicitud")
        await interaction.response.send_message(embed=loading_embed)
        loading_msg = await interaction.original_response()

        # Manejar comando stop
        if query and query.lower() == "stop":
            if interaction.guild.id in dj_sessions:
                dj_sessions[interaction.guild.id]["active"] = False
                task = dj_auto_tasks.pop(interaction.guild.id, None)
                if task and not task.done():
                    task.cancel()
                embed = discord.Embed(
                    title="🎧 Modo DJ Detenido",
                    description="La música continuará hasta que la cola se acabe.",
                    color=0x7289DA
                )
                return await loading_msg.edit(embed=embed)
            else:
                embed = discord.Embed(
                    title="ℹ️ No hay DJ Activo",
                    description="No hay una sesión DJ activa para detener.",
                    color=0x7289DA
                )
                return await loading_msg.edit(embed=embed)

        # Determinar tema
        theme = theme.lower() if theme else detect_theme(query)

        # Iniciar sesión DJ
        init_dj_session(interaction, theme, query)

        # Lógica principal
        try:
            if not query:
                query = await generate_suggestion(interaction)
                if not query:
                    return

            search_terms = await generate_search_terms(query)
            added_songs = await process_search_terms(interaction, search_terms)

            if added_songs == 0:
                raise Exception("No se encontraron canciones adecuadas")

            await handle_playback(interaction, query, added_songs, loading_msg, theme)

        except Exception as e:
            logger.error(f"Error en modo DJ: {e}")
            error_text = str(e)
            if _is_gemini_quota_error(e):
                error_text = "Gemini se quedó sin cuota, pero el DJ ya está configurado para usar fallback local. Reinicia el bot si ves este aviso otra vez."
            error_embed = discord.Embed(
                title="❌ Error en Modo DJ",
                description=f"Ocurrió un error: {error_text[:200]}",
                color=0xE74C3C
            )
            await loading_msg.edit(embed=error_embed)

    except Exception as e:
        logger.error(f"Error en dj_slash: {e}")
        await interaction.followup.send("❌ Ocurrió un error inesperado en el modo DJ")


async def auto_add_songs_task(guild):
    """Añade canciones automáticamente cuando la cola está por terminarse"""
    while guild.id in dj_sessions and dj_sessions[guild.id]["active"]:
        try:
            # Esperar condiciones óptimas
            while True:
                # Verificar si el modo DJ sigue activo
                if guild.id not in dj_sessions or not dj_sessions[guild.id]["active"]:
                    return

                # Verificar estado de la cola
                queue_length = len(queues.get(guild.id, []))
                voice_client = guild.voice_client

                # Condiciones para añadir más canciones:
                # 1. Quedan menos de 2 canciones en cola O
                # 2. No hay nada reproduciéndose actualmente
                if queue_length <= 2 or (
                        voice_client and not voice_client.is_playing() and not voice_client.is_paused()):
                    break

                await asyncio.sleep(5)

            # Obtener contexto simulado
            class SimulatedContext:
                def __init__(self, guild):
                    self.guild = guild
                    self.author = guild.me
                    self.voice_client = guild.voice_client

                @property
                def channel(self):
                    return dj_sessions.get(guild.id, {}).get("origin_channel") or guild.system_channel or next(
                        (c for c in guild.text_channels
                         if c.permissions_for(guild.me).send_messages), None)

            ctx = SimulatedContext(guild)
            query = dj_sessions[guild.id]["last_query"]

            if not query:
                # Generar sugerencia basada en historial
                history = load_history(guild.id)
                if history:
                    prompt = (
                            "Analiza estas canciones recientes:\n" +
                            "\n".join(f"- {song['title']}" for song in history[-5:]) +
                            "\n\nComo DJ experto, genera un término de búsqueda que represente una continuación natural de esta sesión musical. "
                            "Devuelve SOLO el término de búsqueda, nada más."
                    )
                    ai_query = await safe_gemini_text(prompt, context="DJ auto sugerencia")
                    query = ai_query or f"{history[-1].get('title', '')} radio mix"
                    dj_sessions[guild.id]["last_query"] = query

            if query:
                # Generar términos de búsqueda
                prompt = (
                    f"Como DJ musical experto, analiza esta consulta: '{query}'\n"
                    "Genera 3 términos de búsqueda específicos para encontrar música perfectamente relacionada en YouTube.\n"
                    "Formato: término1 | término2 | término3 (sin explicaciones)"
                )
                ai_terms = await safe_gemini_text(prompt, context="DJ auto términos")
                if ai_terms:
                    search_terms = [term.strip() for term in ai_terms.split('|') if term.strip()][:4]
                else:
                    search_terms = build_dj_fallback_terms(query, limit=5)

                if not search_terms:
                    search_terms = build_dj_fallback_terms(query, limit=5)

                # Procesar cada término de búsqueda
                added_songs = 0
                for term in search_terms:
                    try:
                        song = await resolve_song(term, guild.me, max_duration=600)

                        if guild.id in queues and any(s.get('url') == song.get('url') for s in queues[guild.id]):
                            continue

                        queues.setdefault(guild.id, []).append(song)
                        added_songs += 1
                        await asyncio.sleep(1)

                    except Exception as e:
                        logger.warning(f"No se pudo auto-añadir término DJ {term}: {e}")
                        continue

                if added_songs > 0 and ctx.channel:
                    try:
                        await ctx.channel.send(
                            f"🎧 Modo DJ: Añadí {added_songs} nuevas canciones basadas en **{query}**"
                        )
                    except Exception as e:
                        logger.error(f"Error enviando mensaje de auto-add: {e}")

        except Exception as e:
            logger.error(f"Error en auto_add_songs_task: {e}")

        # Esperar antes de verificar nuevamente
        await asyncio.sleep(10)


# Funciones auxiliares para el comando DJ
def detect_theme(query: str) -> str:
    """Detecta el tema musical basado en la consulta"""
    if not query:
        return "default"

    query_lower = query.lower()
    if any(word in query_lower for word in ["fiesta", "party", "baile"]):
        return "party"
    elif any(word in query_lower for word in ["relax", "chill", "tranquilo"]):
        return "chill"
    elif any(word in query_lower for word in ["gym", "workout", "ejercicio"]):
        return "workout"
    elif any(word in query_lower for word in ["focus", "concentración", "estudio"]):
        return "focus"
    return "default"


def init_dj_session(interaction, theme: str, query: str):
    """Inicializa o actualiza la sesión DJ"""
    if interaction.guild.id not in dj_sessions:
        dj_sessions[interaction.guild.id] = {
            "active": True,
            "last_query": query,
            "auto_add": True,
            "theme": theme,
            "origin_channel": interaction.channel,
            "last_add_time": time.time()
        }
    else:
        dj_sessions[interaction.guild.id].update({
            "active": True,
            "auto_add": True,
            "theme": theme,
            "last_query": query or dj_sessions[interaction.guild.id]["last_query"],
            "last_add_time": time.time()
        })


async def generate_suggestion(interaction) -> Optional[str]:
    """Genera una sugerencia basada en el historial"""
    if not current_songs.get(interaction.guild.id) and not queues.get(interaction.guild.id):
        embed = discord.Embed(
            title="🎧 Se necesita una canción inicial",
            description="Especifica un artista o canción para comenzar",
            color=0x7289DA
        )
        await interaction.followup.send(embed=embed)
        return None

    history = load_history(interaction.guild.id)
    if not history:
        embed = discord.Embed(
            title="🎧 Historial Insuficiente",
            description="No tengo suficiente historial para hacer sugerencias",
            color=0x7289DA
        )
        await interaction.followup.send(embed=embed)
        return None

    prompt = (
            "Analiza estas canciones recientes:\n" +
            "\n".join(f"- {song['title']}" for song in history[-5:]) +
            "\n\nComo DJ experto, genera un término de búsqueda para música similar "
            "(solo devuelve el término, nada más)"
    )

    ai_query = await safe_gemini_text(prompt, context="DJ sugerencia slash")
    query = ai_query or f"{history[-1].get('title', '')} radio mix"

    embed = discord.Embed(
        title="🎧 Sugerencia Automática",
        description=f"Basado en tu historial: **{query}**",
        color=0x9147FF
    )
    await interaction.followup.send(embed=embed)

    return query


async def generate_search_terms(query: str) -> List[str]:
    """Genera términos de búsqueda relacionados para DJ sin depender de Gemini."""
    base_terms = build_dj_fallback_terms(query, limit=5)
    if not base_terms:
        return []

    prompt = (
        f"Como DJ musical experto, analiza: '{query}'\n"
        "Genera 4 términos de búsqueda cortos para encontrar canciones relacionadas.\n"
        "Incluye artista/canción si aplica, radio mix, canciones similares y una opción popular.\n"
        "Formato: término1 | término2 | término3 | término4 (sin explicaciones)"
    )

    ai_terms = await safe_gemini_text(prompt, context="DJ términos slash")
    if ai_terms:
        merged = [term.strip() for term in ai_terms.split('|') if term.strip()] + base_terms
    else:
        merged = base_terms

    unique = []
    seen = set()
    for term in merged:
        clean = _clean_search_query(term)
        if clean and clean.lower() not in seen:
            unique.append(clean)
            seen.add(clean.lower())
    return unique[:5] or base_terms


async def process_search_terms(interaction, search_terms: List[str]) -> int:
    """Procesa términos DJ evitando repetir canción actual, cola y canciones recientes."""
    added_songs = 0
    exclusions = get_dj_exclusion_markers(interaction.guild.id)
    for term in search_terms:
        try:
            song = await resolve_song(term, interaction.user, max_duration=600, exclude_markers=exclusions)
            added = await add_song_to_queue(interaction.guild.id, song, avoid_recent=True)
            if added:
                exclusions.update(song_markers(song))
                added_songs += 1
                await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"No se pudo procesar el término {term}: {e}")
            continue

    return added_songs


async def handle_playback(interaction, query: str, added_songs: int, loading_msg, theme: str):
    """Maneja la reproducción y muestra los resultados"""
    voice_client = interaction.guild.voice_client or await safe_connect(interaction.user.voice.channel)

    if not voice_client.is_playing() and not voice_client.is_paused():
        await check_queue(interaction)

    theme_data = {
        "default": {"color": 0x9147FF, "icon": "🎧", "name": "Modo DJ"},
        "party": {"color": 0xFF00FF, "icon": "🎉", "name": "Fiesta"},
        "chill": {"color": 0x00BFFF, "icon": "🌴", "name": "Relajación"},
        "workout": {"color": 0xFF4500, "icon": "💪", "name": "Entrenamiento"},
        "focus": {"color": 0x32CD32, "icon": "🎯", "name": "Concentración"}
    }
    theme_info = theme_data.get(theme, theme_data["default"])

    embed = discord.Embed(
        title=f"{theme_info['icon']} {theme_info['name']} - Activado",
        description=f"**Tema seleccionado:** {query}\n"
                    f"**Canciones añadidas:** {added_songs}\n"
                    f"**Modo:** Auto-reproducción activada",
        color=theme_info["color"]
    )

    # Mostrar las próximas 3 canciones
    queue_preview = queues[interaction.guild.id][-added_songs:][:3]
    for i, song in enumerate(queue_preview, 1):
        duration = f"{song['duration'] // 60}:{song['duration'] % 60:02d}" if song['duration'] > 0 else "Desconocida"
        embed.add_field(
            name=f"{theme_info['icon']} Próxima {i}",
            value=f"[{song['title']}]({song['web_url']})\n"
                  f"**Artista:** {song['uploader']}\n"
                  f"**Duración:** {duration}",
            inline=False
        )

    embed.set_thumbnail(url=queue_preview[0]['thumbnail'] if queue_preview else None)
    embed.set_footer(
        text=f"DJ: {interaction.user.display_name} | Usa /dj stop para finalizar",
        icon_url=interaction.user.display_avatar.url
    )

    await loading_msg.edit(embed=embed)

    if dj_sessions[interaction.guild.id]["auto_add"]:
        ensure_dj_auto_task(interaction.guild)

@bot.tree.command(name="charla", description="Interactúa con la IA de Google Gemini con memoria contextual mejorada")
@app_commands.describe(mensaje="Tu mensaje para la IA")
async def charla_slash(ctx: discord.Interaction, mensaje: str):
    """Interactúa con la IA de Google Gemini con memoria contextual mejorada."""
    user_id = str(ctx.user.id)

    # Respuestas rápidas
    quick_responses = {
        "¿cómo te llamas?": "🤖 ¡Soy Archeon, tu asistente de Discord! ✨",
        "¿quién eres?": "🤖 ¡Soy Archeon, tu asistente de Discord! ✨",
        "¿cuál es tu nombre?": "🤖 ¡Soy Archeon, tu asistente de Discord! ✨",
        "¿quién soy?": f"🤖 ¡Claro que te conozco, {ctx.user.mention}! Eres {ctx.user.name} 😊",
        "¿cómo me llamo?": f"🤖 ¡Claro que te conozco, {ctx.user.mention}! Eres {ctx.user.name} 😊",
        "¿me conoces?": f"🤖 ¡Claro que te conozco, {ctx.user.mention}! Eres {ctx.user.name} 😊"
    }

    lower_msg = mensaje.lower().strip()
    if lower_msg in quick_responses:
        return await ctx.response.send_message(quick_responses[lower_msg])

    try:
        # Inicializar historial si es nuevo usuario
        if user_id not in chat_histories:
            chat_histories[user_id] = []

        # Construir contexto
        context = {
            "historial": "\n".join(chat_histories[user_id][-MAX_HISTORY:]),
            "nuevo_mensaje": mensaje,
            "usuario": ctx.user.name
        }

        prompt = (
            "Eres un asistente de Discord llamado Archeon. "
            "Aquí está el historial de conversación reciente:\n"
            "{historial}\n\n"
            "Nuevo mensaje de {usuario}: {nuevo_mensaje}\n\n"
            "Responde de manera concisa y amigable."
        ).format(**context)

        # Generar respuesta
        if model is None:
            return await ctx.response.send_message("❌ Falta configurar `GOOGLE_API_KEY` en el archivo `.env`.", ephemeral=True)
        response = model.generate_content(prompt)
        respuesta = response.text.strip()

        # Actualizar historial
        chat_histories[user_id].extend([
            f"{ctx.user.name}: {mensaje}",
            f"Archeon: {respuesta}"
        ])
        chat_histories[user_id] = chat_histories[user_id][-MAX_HISTORY:]

        # Enviar respuesta
        await ctx.response.send_message(f"{ctx.user.mention} {respuesta}")

    except genai.errors.GoogleAPIError as api_error:
        await ctx.response.send_message("🔴 Error con la API de Google. Por favor, reporta esto al administrador.")
        logger.error(f"Google API Error: {api_error}")

    except Exception as e:
        logger.error(f"Error inesperado: {e}", exc_info=True)
        await ctx.response.send_message("⚠️ Ocurrió un error inesperado. Por favor, intenta nuevamente más tarde.")


# Comando para reiniciar historial (slash command)
@bot.tree.command(name="olvidar", description="Reinicia el historial de conversación contigo")
async def olvidar_slash(ctx: discord.Interaction):
    """Reinicia el historial de conversación contigo"""
    user_id = str(ctx.user.id)
    if user_id in chat_histories:
        chat_histories[user_id] = []
    await ctx.response.send_message("🔄 ¡He reiniciado nuestra conversación! ¿En qué puedo ayudarte ahora?")


# Comando para separar jugadores (slash command)
@bot.tree.command(name="separar",
                  description="Separa a los usuarios en canales de voz según el juego que están jugando")
async def separar_jugadores_slash(ctx: discord.Interaction):
    """Separa a los usuarios en canales de voz según el juego que están jugando"""
    try:
        # Verificar que el comando se ejecuta en un servidor
        if not ctx.guild:
            await ctx.response.send_message("❌ Este comando solo funciona en servidores.", ephemeral=True)
            return

        # Verificar que el usuario está en un canal de voz
        if not ctx.user.voice or not ctx.user.voice.channel:
            await ctx.response.send_message("❌ Debes estar en un canal de voz para usar este comando.", ephemeral=True)
            return

        await ctx.response.defer()

        voice_channel = ctx.user.voice.channel
        members = voice_channel.members

        # Obtener los juegos activos entre los miembros
        juegos_activos = {}
        for member in members:
            if member.activity and member.activity.type == discord.ActivityType.playing:
                juego = member.activity.name
                if juego not in juegos_activos:
                    juegos_activos[juego] = []
                juegos_activos[juego].append(member)

        # Si no hay suficientes juegos diferentes
        if len(juegos_activos) < 2:
            await ctx.followup.send("🔍 No hay suficientes juegos diferentes para separar (se necesitan al menos 2).")
            return

        # Consultar a la IA para nombres creativos de canales
        prompt = (
            f"Dame nombres creativos para canales de Discord basados en estos juegos: {', '.join(juegos_activos.keys())}. "
            "Los nombres deben ser cortos, relevantes al juego y entre 3-5 palabras. "
            "Formato: Juego: Nombre sugerido (uno por juego)"
        )

        try:
            response = model.generate_content(prompt)
            nombres_canales = {}

            # Parsear la respuesta de la IA
            for line in response.text.split('\n'):
                if ':' in line:
                    juego, nombre = line.split(':', 1)
                    juego = juego.strip()
                    nombre = nombre.strip()
                    if juego in juegos_activos:
                        nombres_canales[juego] = nombre
        except Exception as e:
            logging.error(f"Error al generar nombres con IA: {str(e)}")
            # Usar nombres por defecto si falla la IA
            nombres_canales = {juego: f"🎮 {juego}" for juego in juegos_activos}

        # Crear categoría temporal si no existe
        categoria = discord.utils.get(ctx.guild.categories, name="Juegos Temporales")
        if not categoria:
            categoria = await ctx.guild.create_category_channel("Juegos Temporales")

        # Crear canales de voz temporales
        canales_creados = {}
        for juego, nombre in nombres_canales.items():
            try:
                # Limitar longitud del nombre a 100 caracteres (límite de Discord)
                nombre_canal = nombre[:100]
                new_channel = await ctx.guild.create_voice_channel(
                    name=nombre_canal,
                    category=categoria,
                    reason=f"Separación automática por juego: {juego}"
                )
                canales_creados[juego] = new_channel
            except Exception as e:
                logging.error(f"Error al crear canal para {juego}: {str(e)}")
                continue

        # Mover usuarios a los canales correspondientes
        movimientos = {}
        for juego, miembros in juegos_activos.items():
            if juego in canales_creados:
                canal_destino = canales_creados[juego]
                for miembro in miembros:
                    try:
                        await miembro.move_to(canal_destino)
                        if juego not in movimientos:
                            movimientos[juego] = 0
                        movimientos[juego] += 1
                    except Exception as e:
                        logging.error(f"Error al mover {miembro.display_name}: {str(e)}")

        # Enviar resumen
        resumen = "✅ Separación completada:\n"
        for juego, count in movimientos.items():
            resumen += f"- {juego}: {count} jugadores movidos a {canales_creados[juego].mention}\n"

        await ctx.followup.send(resumen)

        # Programar eliminación de canales después de inactividad
        await asyncio.sleep(300)  # Esperar 5 minutos

        # Verificar si los canales están vacíos
        for juego, canal in canales_creados.items():
            if len(canal.members) == 0:
                try:
                    await canal.delete(reason="Canal temporal de juego vacío")
                except Exception as e:
                    logging.error(f"Error al eliminar canal {canal.name}: {str(e)}")

    except Exception as e:
        logging.error(f"Error en comando separar: {str(e)}\n{traceback.format_exc()}")
        await ctx.followup.send("❌ Ocurrió un error al procesar el comando. Por favor intenta nuevamente.")


@bot.tree.command(name="votar", description="Crea encuestas con o sin tiempo personalizado")
@app_commands.describe(
    tiempo_minutos="Tiempo en minutos (opcional, por defecto 1)",
    pregunta="La pregunta para la votación",
    opcion1="Primera opción",
    opcion2="Segunda opción",
    opcion3="Tercera opción (opcional)",
    opcion4="Cuarta opción (opcional)",
    opcion5="Quinta opción (opcional)",
    opcion6="Sexta opción (opcional)"
)
async def votar_slash(
        ctx: discord.Interaction,
        pregunta: str,
        opcion1: str,
        opcion2: str,
        tiempo_minutos: int = 1,
        opcion3: str = None,
        opcion4: str = None,
        opcion5: str = None,
        opcion6: str = None
):
    """Crea encuestas con o sin tiempo personalizado"""
    # Configuración inicial
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣']

    # Recolectar opciones válidas
    opciones = [op for op in [opcion1, opcion2, opcion3, opcion4, opcion5, opcion6] if op is not None]

    # Validaciones
    if len(opciones) < 2:
        return await ctx.response.send_message("❌ Necesitas al menos 2 opciones.", ephemeral=True)
    if tiempo_minutos <= 0:
        return await ctx.response.send_message("❌ El tiempo debe ser mayor a 0 minutos.", ephemeral=True)

    # Crear embed
    embed = discord.Embed(
        title=f"📊 {pregunta}",
        description="\n".join([f"{emojis[i]} {op}" for i, op in enumerate(opciones)]),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"⏳ Votación abierta por {tiempo_minutos} minuto(s)")

    # Enviar y añadir reacciones
    await ctx.response.send_message(embed=embed)
    mensaje = await ctx.original_response()

    for i in range(len(opciones)):
        await mensaje.add_reaction(emojis[i])

    # Esperar y calcular resultados
    await asyncio.sleep(tiempo_minutos * 60)
    mensaje_actualizado = await ctx.channel.fetch_message(mensaje.id)

    resultados = {}
    for i, emoji in enumerate(emojis[:len(opciones)]):
        for reaccion in mensaje_actualizado.reactions:
            if str(reaccion.emoji) == emoji:
                resultados[opciones[i]] = reaccion.count - 1

    # Determinar ganador
    if not resultados:
        return await ctx.followup.send("🤷 Nadie votó.")

    ganador = max(resultados.items(), key=lambda x: x[1])
    porcentaje = (ganador[1] / sum(resultados.values())) * 100

    # Generar comentario con IA
    try:
        respuesta = model.generate_content(
            f"Crea un comentario gracioso (1 línea) sobre esta votación: "
            f"'{pregunta}'. Ganador: '{ganador[0]}' con {porcentaje:.1f}% votos."
        )
        comentario = respuesta.text
    except Exception:
        comentario = "¡Y el veredicto es...!"

    # Mostrar resultados
    embed_resultado = discord.Embed(
        title=f"🎉 Ganador: {ganador[0]} ({porcentaje:.1f}%)",
        description=f"**{pregunta}**\n\n{comentario}",
        color=discord.Color.green()
    )
    await ctx.followup.send(embed=embed_resultado)



@bot.tree.command(name="mantener_voz", description="Evita que el bot se desconecte solo del canal de voz")
@app_commands.describe(modo="on para activar, off para desactivar")
async def mantener_voz_slash(interaction: discord.Interaction, modo: str = "on"):
    modo = (modo or "on").lower().strip()
    if modo in {"on", "si", "sí", "true", "1", "activar"}:
        voice_stay_connected_guilds.add(interaction.guild.id)
        await interaction.response.send_message("✅ Modo **mantener voz** activado. No me bajo solo de la llamada.")
    elif modo in {"off", "no", "false", "0", "desactivar"}:
        voice_stay_connected_guilds.discard(interaction.guild.id)
        await interaction.response.send_message("✅ Modo **mantener voz** desactivado.")
    else:
        estado = "activado" if interaction.guild.id in voice_stay_connected_guilds else "desactivado"
        await interaction.response.send_message(f"ℹ️ Uso: `/mantener_voz modo:on/off`. Estado actual: **{estado}**.", ephemeral=True)


@bot.tree.command(name="voz_estado", description="Muestra diagnóstico rápido de voz y música")
async def voz_estado_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    embed = discord.Embed(title="🔎 Estado de voz", color=discord.Color.blurple())
    embed.add_field(name="Conectado", value="Sí" if vc and vc.is_connected() else "No", inline=True)
    embed.add_field(name="Reproduciendo", value="Sí" if vc and vc.is_playing() else "No", inline=True)
    embed.add_field(name="Pausado", value="Sí" if vc and vc.is_paused() else "No", inline=True)
    embed.add_field(name="Canal guardado", value=str(last_voice_channel_ids.get(interaction.guild.id, "Ninguno")), inline=False)
    embed.add_field(name="Actual", value=(current_songs.get(interaction.guild.id, {}) or {}).get("title", "Nada"), inline=False)
    embed.add_field(name="Cola", value=str(len(queues.get(interaction.guild.id, []))), inline=True)
    embed.add_field(name="Mantener voz", value="Sí" if interaction.guild.id in voice_stay_connected_guilds else "No", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------
# Slash: Diversión
# --------------------------

@bot.tree.command(name="insultar", description="Roast creativo y fuertecito para un usuario")
@app_commands.describe(usuario="Usuario que recibirá el roast", razon="Motivo opcional")
async def insultar_slash(interaction: discord.Interaction, usuario: discord.Member, razon: Optional[str] = None):
    try:
        insulto = await generar_roast_limpio(usuario, razon)
        await interaction.response.send_message(
            f"{usuario.mention}, {insulto}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )

    except Exception as e:
        logger.error(f"Error en insultar_slash: {e}")
        await interaction.response.send_message(
            f"{usuario.mention}, {sanitize_roast_text(random.choice(ROASTS_FUERTES), usuario)}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )


@bot.tree.command(name="abrazo", description="Manda un abrazo virtual")
@app_commands.describe(usuario="Usuario que recibirá el abrazo")
async def abrazo_slash(interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
    usuario = usuario or interaction.user
    embed = discord.Embed(
        title="🫂 Abrazo enviado",
        description=f"{interaction.user.mention} le mandó un abrazo a {usuario.mention}.",
        color=0xFFB6C1
    )
    embed.set_image(url=random.choice(HUG_GIFS))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="meme", description="Genera un meme en español con imagen")
async def meme_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        title, file = build_spanish_meme_file()
        await interaction.followup.send(content=f"😂 **{title}**", file=file)
    except Exception as e:
        logger.error(f"Error generando /meme con imagen: {traceback.format_exc()}")
        await interaction.followup.send(f"❌ No pude generar la imagen del meme: {str(e)[:300]}", ephemeral=True)



@bot.tree.command(name="chiste", description="Cuenta un chiste corto")
async def chiste_slash(interaction: discord.Interaction):
    await interaction.response.send_message(f"😄 {random.choice(JOKES)}")


@bot.tree.command(name="moneda", description="Lanza una moneda")
async def moneda_slash(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 Salió **{random.choice(['cara', 'cruz'])}**.")


@bot.tree.command(name="dado", description="Lanza un dado")
@app_commands.describe(caras="Número de caras del dado, entre 2 y 100")
async def dado_slash(interaction: discord.Interaction, caras: int = 6):
    caras = max(2, min(caras, 100))
    await interaction.response.send_message(f"🎲 D{caras}: **{random.randint(1, caras)}**")


@bot.tree.command(name="bola8", description="Pregunta algo a la bola 8")
@app_commands.describe(pregunta="Tu pregunta")
async def bola8_slash(interaction: discord.Interaction, pregunta: str):
    await interaction.response.send_message(f"🎱 **Pregunta:** {pregunta}\n**Respuesta:** {random.choice(EIGHT_BALL_RESPONSES)}")


@bot.tree.command(name="elegir", description="Elige entre opciones separadas por coma")
@app_commands.describe(opciones="Opciones separadas por coma")
async def elegir_slash(interaction: discord.Interaction, opciones: str):
    partes = [op.strip() for op in opciones.split(',') if op.strip()]
    if len(partes) < 2:
        return await interaction.response.send_message("❌ Dame al menos 2 opciones separadas por coma.", ephemeral=True)
    await interaction.response.send_message(f"🤔 Elijo: **{random.choice(partes)}**")


@bot.tree.command(name="ship", description="Mira qué tanto se quieren esos dos 😳")
@app_commands.describe(usuario1="Primer usuario", usuario2="Segundo usuario opcional")
async def ship_slash(interaction: discord.Interaction, usuario1: discord.Member, usuario2: Optional[discord.Member] = None):
    usuario2 = usuario2 or interaction.user
    porcentaje = random.randint(0, 100)
    embed = build_ship_embed(usuario1, usuario2, porcentaje)
    file = build_ship_card_file(usuario1, usuario2, porcentaje)
    if file:
        embed.set_image(url="attachment://shipometro.png")
        await interaction.response.send_message(embed=embed, file=file)
    else:
        await interaction.response.send_message(embed=embed)




# ----------------------------------------------------
# Comandos slash extra para que el menú "/" quede completo
# ----------------------------------------------------

@bot.tree.command(name="play", description="Alias de /reproducir para poner música")
@app_commands.describe(busqueda="Nombre, URL o búsqueda de la canción")
async def play_alias_slash(interaction: discord.Interaction, busqueda: str):
    await play_slash(interaction, busqueda=busqueda)


@bot.tree.command(name="unirse", description="Hace que el bot entre a tu canal de voz")
async def unirse_slash(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)

    try:
        voice_client = await safe_connect(interaction.user.voice.channel)
        try:
            voice_client.encoder_options = {
                'channels': 2,
                'frame_length': 60,
                'sample_rate': 48000,
                'bitrate': '128k'
            }
        except Exception:
            pass
        await interaction.response.send_message(f"🔊 Conectado a **{interaction.user.voice.channel.name}**")
    except Exception as e:
        logger.error(f"Error en /unirse: {traceback.format_exc()}")
        await interaction.response.send_message(human_voice_error(e), ephemeral=True)


@bot.tree.command(name="historial", description="Muestra el historial musical del servidor")
@app_commands.describe(fecha="Opcional: fecha en formato YYYY-MM-DD")
async def historial_slash(interaction: discord.Interaction, fecha: Optional[str] = None):
    if fecha:
        try:
            datetime.strptime(fecha, "%Y-%m-%d")
        except ValueError:
            return await interaction.response.send_message("❌ Formato inválido. Usa `YYYY-MM-DD`.", ephemeral=True)

    history = load_history(interaction.guild.id, fecha)
    if not history:
        return await interaction.response.send_message("📭 No hay historial para esa fecha." if fecha else "📭 No hay historial de hoy.", ephemeral=True)

    embed = discord.Embed(
        title=f"📜 Historial de reproducción ({fecha or 'hoy'})",
        description=f"Total: {len(history)} canciones",
        color=discord.Color.dark_gold()
    )

    for i, song in enumerate(history[-10:][::-1], 1):
        duration = ""
        if song.get('duration', 0) > 0:
            mins, secs = divmod(song['duration'], 60)
            duration = f" [{mins}:{secs:02d}]"
        embed.add_field(
            name=f"{i}. {song.get('title', 'Canción sin título')}{duration}",
            value=f"Solicitado por: {song.get('requested_by', 'Desconocido')}",
            inline=False
        )

    if len(history) > 10:
        embed.set_footer(text=f"Mostrando las últimas 10 de {len(history)} canciones")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="imagen", description="Genera una imagen con IA desde una descripción en español")
@app_commands.describe(descripcion="Describe la imagen que quieres generar")
async def imagen_slash(interaction: discord.Interaction, descripcion: str):
    if not STABILITY_API_KEY:
        return await interaction.response.send_message("❌ Falta configurar `STABILITY_API_KEY` en `.env`.", ephemeral=True)

    await interaction.response.defer(thinking=True)
    try:
        descripcion_en = GoogleTranslator(source='es', target='en').translate(descripcion)
        payload = {
            "text_prompts": [{"text": descripcion_en, "weight": 1.0}],
            "cfg_scale": 7,
            "height": 512,
            "width": 512,
            "samples": 1,
            "steps": 30
        }
        headers = {
            "Authorization": f"Bearer {STABILITY_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            async with session.post(
                "https://api.stability.ai/v1/generation/stable-diffusion-v1-6/text-to-image",
                headers=headers,
                json=payload
            ) as response:
                try:
                    data = await response.json()
                except Exception:
                    data = {"message": await response.text()}

                if response.status != 200:
                    return await interaction.followup.send(f"❌ Error en Stability: {data.get('message', 'Error desconocido')}")

        if "artifacts" not in data or not data["artifacts"]:
            return await interaction.followup.send("⚠️ No se pudo generar la imagen. Prueba con otra descripción.")

        image_data = base64.b64decode(data["artifacts"][0]["base64"])
        with io.BytesIO(image_data) as image_buffer:
            await interaction.followup.send(file=discord.File(fp=image_buffer, filename="imagen.png"))

    except Exception:
        logger.error(f"Error en /imagen: {traceback.format_exc()}")
        await interaction.followup.send("❌ Ocurrió un error al generar la imagen.")


@bot.tree.command(name="borrarchat", description="Borra mensajes del bot en tu DM")
async def borrarchat_slash(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.DMChannel):
        return await interaction.response.send_message("❌ Este comando solo funciona en mensajes directos con el bot.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    deleted_count = 0
    async for msg in interaction.channel.history(limit=200):
        if msg.author == bot.user:
            try:
                await msg.delete()
                deleted_count += 1
                await asyncio.sleep(0.5)
            except Exception:
                continue
    await interaction.followup.send(f"✅ Borré **{deleted_count}** mensajes del bot en este DM.", ephemeral=True)


@bot.tree.command(name="limpiar", description="Borra mensajes del canal actual")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(cantidad="Cantidad de mensajes a borrar, entre 1 y 100")
async def limpiar_slash(interaction: discord.Interaction, cantidad: int = 10):
    if not interaction.guild:
        return await interaction.response.send_message("❌ Este comando solo funciona en servidores.", ephemeral=True)

    cantidad = max(1, min(100, cantidad))
    await interaction.response.defer(ephemeral=True)

    try:
        deleted = await interaction.channel.purge(limit=cantidad, check=lambda m: not m.pinned)
        await interaction.followup.send(f"🧹 Eliminados **{len(deleted)}** mensajes.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ No tengo permisos para borrar mensajes.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en /limpiar: {traceback.format_exc()}")
        await interaction.followup.send(f"❌ Error: {str(e)[:200]}", ephemeral=True)


@bot.tree.command(name="silenciar", description="Asigna el rol Silenciado a un usuario")
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(usuario="Usuario a silenciar", razon="Razón opcional")
async def silenciar_slash(interaction: discord.Interaction, usuario: discord.Member, razon: str = "Sin razón"):
    if not interaction.guild:
        return await interaction.response.send_message("❌ Este comando solo funciona en servidores.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        role = discord.utils.get(interaction.guild.roles, name="Silenciado")
        if not role:
            role = await interaction.guild.create_role(name="Silenciado")
            for channel in interaction.guild.channels:
                try:
                    await channel.set_permissions(role, send_messages=False)
                except Exception:
                    pass

        await usuario.add_roles(role, reason=razon)
        embed = discord.Embed(
            title=f"🔇 {usuario.display_name} silenciado",
            description=f"Razón: {razon}",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
    except discord.Forbidden:
        await interaction.followup.send("❌ No tengo permisos suficientes para silenciar.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en /silenciar: {traceback.format_exc()}")
        await interaction.followup.send(f"❌ Error: {str(e)[:200]}", ephemeral=True)


@bot.tree.command(name="eliminar_strikes", description="Elimina los strikes registrados de un usuario")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(usuario="Usuario al que se le limpiarán los strikes")
async def eliminar_strikes_slash(interaction: discord.Interaction, usuario: discord.Member):
    user_id = usuario.id
    if user_id not in user_warnings:
        return await interaction.response.send_message(f"ℹ️ {usuario.mention} no tiene strikes registrados.", ephemeral=True)

    del user_warnings[user_id]
    save_moderation_data()

    embed = discord.Embed(
        title="✅ Strikes eliminados",
        description=f"Se removieron todos los strikes de {usuario.mention}.",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Moderador: {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

    try:
        await usuario.send(f"♻️ Tus strikes han sido reiniciados por {interaction.user.mention} en {interaction.guild.name}")
    except discord.Forbidden:
        pass


# --------------------------
# Eventos del bot
# --------------------------


@bot.tree.command(name="karaoke", description="Control del modo karaoke")
@app_commands.describe(
    accion="Qué quieres hacer",
    cancion="Canción para apuntarte cuando usas accion=entrar",
    usuario="Usuario a puntuar cuando usas accion=puntuar",
    puntos="Puntaje 0-100 cuando usas accion=puntuar",
    comentario="Comentario opcional para la puntuación"
)
@app_commands.choices(accion=[
    app_commands.Choice(name="iniciar", value="iniciar"),
    app_commands.Choice(name="entrar", value="entrar"),
    app_commands.Choice(name="cola", value="cola"),
    app_commands.Choice(name="comenzar", value="comenzar"),
    app_commands.Choice(name="puntuar", value="puntuar"),
    app_commands.Choice(name="saltar", value="saltar"),
    app_commands.Choice(name="finalizar", value="finalizar"),
])
async def karaoke_slash(
    interaction: discord.Interaction,
    accion: app_commands.Choice[str],
    cancion: Optional[str] = None,
    usuario: Optional[discord.Member] = None,
    puntos: Optional[int] = None,
    comentario: Optional[str] = None
):
    if not interaction.guild:
        return await interaction.response.send_message("❌ Este comando solo funciona en servidores.", ephemeral=True)

    value = accion.value

    if value == "iniciar":
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)
        KARAOKE_SESSIONS[interaction.guild.id] = {
            "host_id": interaction.user.id,
            "host_channel_id": interaction.user.voice.channel.id,
            "participants": set([interaction.user.id]),
            "queue": [],
            "history": [],
            "current": None,
            "running": False,
            "created_at": time.time()
        }
        await interaction.response.send_message("🎤 Karaoke creado. Usa `/karaoke entrar` con tu canción.")
        try:
            await safe_connect(interaction.user.voice.channel)
        except Exception:
            pass
        return

    session = _karaoke_get_session(interaction.guild.id)
    if not session:
        return await interaction.response.send_message("❌ No hay karaoke activo. Usa `/karaoke iniciar` primero.", ephemeral=True)

    if value == "entrar":
        if not cancion:
            return await interaction.response.send_message("❌ Escribe una canción en el campo `cancion`.", ephemeral=True)
        if _karaoke_user_entry(session, interaction.user.id):
            return await interaction.response.send_message("⚠️ Ya tienes turno registrado.", ephemeral=True)
        await interaction.response.defer()
        try:
            song = await resolve_song(cancion, interaction.user)
            session.setdefault("participants", set()).add(interaction.user.id)
            session.setdefault("queue", []).append({
                "member_id": interaction.user.id,
                "display_name": interaction.user.display_name,
                "song_title": song["title"],
                "url": song["url"],
                "web_url": song["web_url"],
                "thumbnail": song.get("thumbnail"),
                "duration": song.get("duration", 0),
                "score": None,
                "comment": None
            })
            await interaction.followup.send(f"✅ {interaction.user.mention} quedó apuntado con **{song['title']}**.")
        except Exception as e:
            await interaction.followup.send(f"❌ No pude agregar esa canción: {str(e)[:500]}")
        return

    if value == "cola":
        embed = discord.Embed(title="🎤 Cola de Karaoke", color=0xFF2D95)
        current = session.get("current")
        if current:
            embed.add_field(name="Cantando ahora", value=f"<@{current['member_id']}> — **{current['song_title']}**", inline=False)
        if session.get("queue"):
            embed.description = "\n".join([f"{i}. <@{e['member_id']}> — **{e['song_title']}**" for i, e in enumerate(session["queue"], 1)][:15])
        else:
            embed.description = embed.description or "No hay canciones pendientes."
        return await interaction.response.send_message(embed=embed)

    if value == "comenzar":
        if not session.get("queue"):
            return await interaction.response.send_message("📭 No hay canciones en cola.", ephemeral=True)
        await interaction.response.defer()
        # mover participantes al canal del host
        class _CtxAdapter:
            pass
        ctx_adapter = _CtxAdapter()
        ctx_adapter.guild = interaction.guild
        ctx_adapter.author = interaction.user
        ctx_adapter.channel = interaction.channel
        ctx_adapter.voice_client = interaction.guild.voice_client
        moved = 0
        try:
            moved = await karaoke_move_participants(ctx_adapter, session)
        except Exception:
            moved = 0
        session["running"] = True
        await interaction.followup.send(f"🎬 Karaoke iniciado. Moví **{moved}** participante(s).")
        await karaoke_start_next(interaction)
        return

    if value == "puntuar":
        if not usuario or puntos is None:
            return await interaction.response.send_message("❌ Usa `usuario` y `puntos` para puntuar.", ephemeral=True)
        if puntos < 0 or puntos > 100:
            return await interaction.response.send_message("❌ La puntuación debe estar entre 0 y 100.", ephemeral=True)
        entry = _karaoke_user_entry(session, usuario.id)
        if not entry:
            return await interaction.response.send_message("❌ Ese usuario no está en el karaoke.", ephemeral=True)
        entry["score"] = puntos
        entry["comment"] = comentario or await karaoke_ai_comment(usuario.display_name, puntos, winner=puntos >= 90)
        return await interaction.response.send_message(f"✅ {usuario.mention} recibió **{puntos}/100**. 💬 {entry['comment']}")

    if value == "saltar":
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()
        else:
            await karaoke_start_next(interaction)
        return await interaction.response.send_message("⏭️ Turno saltado.")

    if value == "finalizar":
        await interaction.response.defer()
        session["running"] = False
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()
        await karaoke_show_results(interaction, session)
        KARAOKE_SESSIONS.pop(interaction.guild.id, None)
        return



# --------------------------
# Bienvenida visual
# --------------------------

async def build_welcome_card(member: discord.Member) -> Optional[discord.File]:
    """Genera una imagen de bienvenida tipo banner."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps

        width, height = 1100, 420
        img = Image.new("RGB", (width, height), (17, 24, 39))
        draw = ImageDraw.Draw(img)

        # Fondo con bandas sutiles.
        for y in range(height):
            shade = int(24 + (y / height) * 24)
            draw.line([(0, y), (width, y)], fill=(17, shade, 52))

        # Decoración.
        draw.rounded_rectangle((35, 35, width - 35, height - 35), radius=32, outline=(245, 158, 11), width=4)
        draw.rounded_rectangle((70, 280, width - 70, 350), radius=24, fill=(31, 41, 55))

        def font(size: int, bold: bool = False):
            candidates = [
                "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
                "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
            for fp in candidates:
                try:
                    if fp and os.path.exists(fp):
                        return ImageFont.truetype(fp, size)
                except Exception:
                    continue
            return ImageFont.load_default()

        title_font = font(56, True)
        name_font = font(42, True)
        text_font = font(28)
        small_font = font(22)

        # Avatar circular.
        avatar_bytes = await member.display_avatar.replace(size=256, static_format="png").read()
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((180, 180))
        mask = Image.new("L", avatar.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, 180, 180), fill=255)
        avatar = ImageOps.fit(avatar, (180, 180), centering=(0.5, 0.5))
        img.paste(avatar, (80, 100), mask)
        draw.ellipse((76, 96, 264, 284), outline=(245, 158, 11), width=6)

        guild_name = member.guild.name[:32]
        member_count = member.guild.member_count or len(member.guild.members)
        display = member.display_name[:28]

        draw.text((300, 95), "¡Bienvenido/a!", font=title_font, fill=(255, 255, 255))
        draw.text((300, 165), display, font=name_font, fill=(245, 158, 11))
        draw.text((300, 225), f"Ahora formas parte de {guild_name}", font=text_font, fill=(229, 231, 235))
        draw.text((95, 300), f"Miembro #{member_count}  •  Pásala bien y respeta las reglas 😎", font=text_font, fill=(255, 255, 255))
        draw.text((70, 365), "Archeon Welcome System", font=small_font, fill=(156, 163, 175))

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(buffer, filename="bienvenida_archeon.png")
    except Exception:
        logger.error(f"No pude generar card de bienvenida: {traceback.format_exc()}")
        return None


@bot.event
async def on_member_join(member: discord.Member):
    """Da una bienvenida visual en el canal configurado."""
    if not WELCOME_ENABLED:
        return

    channel = member.guild.get_channel(WELCOME_CHANNEL_ID) or bot.get_channel(WELCOME_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        logger.warning(f"No encontré canal de bienvenida con ID {WELCOME_CHANNEL_ID}.")
        return

    try:
        embed = discord.Embed(
            title=f"🌟 ¡Bienvenido/a, {member.display_name}!",
            description=(
                f"{member.mention}, llegaste a **{member.guild.name}**.\n\n"
                "📌 Lee las reglas, saluda sin miedo y disfruta el server.\n"
                f"👥 Ahora somos **{member.guild.member_count}** miembros."
            ),
            color=0xF59E0B
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Archeon • Sistema de bienvenida")

        card = await build_welcome_card(member)
        if card:
            embed.set_image(url="attachment://bienvenida_archeon.png")
            await channel.send(content=f"🎉 {member.mention}", embed=embed, file=card)
        else:
            await channel.send(content=f"🎉 {member.mention}", embed=embed)

    except Exception:
        logger.error(f"Error enviando bienvenida: {traceback.format_exc()}")



@bot.event
async def on_ready():
    """Evento mejorado cuando el bot está listo"""
    print(f'✅ Bot conectado como {bot.user.name} (ID: {bot.user.id})')
    print(f'🔄 Sincronizado con {len(bot.guilds)} servidores')

    # Configurar manejo de señales para cierre limpio
    import signal
    def handle_signal(signum, frame):
        logger.info(f"Recibida señal {signum}, cerrando limpiamente...")
        create_logged_task(bot.close(), "bot_close_signal")
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Cargar datos iniciales
    try:
        load_moderation_data()
        await load_malicious_domains()
        logger.info("Datos iniciales cargados correctamente")
    except Exception as e:
        logger.error(f"Error cargando datos iniciales: {e}")

    # Registrar vistas persistentes y publicar panel de tickets.
    try:
        if not getattr(bot, "ticket_view_registered", False):
            bot.add_view(TicketOpenView())
            bot.ticket_view_registered = True

        for guild in bot.guilds:
            await ensure_ticket_panel(guild)
    except Exception as e:
        logger.error(f"Error preparando panel de tickets: {e}")

    # Sincronizar comandos slash con reintentos.
    # Primero global, luego por servidor para que Discord los muestre casi al instante.
    max_retries = 3
    for attempt in range(max_retries):
        try:
            synced = await bot.tree.sync()
            logger.info(f"✅ Comandos slash globales sincronizados: {len(synced)} comandos")

            guild_total = 0
            for guild in bot.guilds:
                try:
                    guild_obj = discord.Object(id=guild.id)
                    bot.tree.copy_global_to(guild=guild_obj)
                    guild_synced = await bot.tree.sync(guild=guild_obj)
                    guild_total += len(guild_synced)
                    logger.info(f"✅ Slash sincronizados en {guild.name}: {len(guild_synced)} comandos")
                except Exception as guild_error:
                    logger.error(f"❌ Error sincronizando slash en {guild.name}: {guild_error}")

            logger.info(f"✅ Sincronización slash completa: global={len(synced)} | por servidor={guild_total}")
            break
        except Exception as e:
            logger.error(f"❌ Error sincronizando comandos slash (intento {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                logger.error("No se pudo sincronizar los comandos slash después de varios intentos")
            await asyncio.sleep(5)

    # Configurar estado del bot
    try:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"🎶 usa ¡ayuda o /ayuda • archeon.bot ",
                status=discord.Status.online
            )
        )
    except Exception as e:
        logger.error(f"Error al cambiar presencia: {e}")

    # Iniciar tareas en segundo plano una sola vez. on_ready puede dispararse de nuevo al reconectar.
    if not getattr(bot, "background_tasks_started", False):
        async def safe_background_task(task_func, *args):
            while True:
                try:
                    await task_func(*args)
                except Exception as e:
                    logger.error(f"Error en tarea en segundo plano {task_func.__name__}: {e}")
                    await asyncio.sleep(60)  # Esperar antes de reintentar

        create_logged_task(safe_background_task(check_empty_voice_channels), "check_empty_voice_channels")
        create_logged_task(safe_background_task(music_voice_watchdog), "music_voice_watchdog")
        create_logged_task(safe_background_task(save_data_periodically), "save_data_periodically")
        bot.background_tasks_started = True
        logger.info("Tareas en segundo plano iniciadas con manejo seguro")
    else:
        logger.info("Tareas en segundo plano ya estaban activas; no se duplican.")


@bot.event
async def on_command(ctx: commands.Context):
    """Cualquier comando cuenta como uso del bot para el temporizador de voz."""
    try:
        if ctx.guild:
            if getattr(ctx.channel, "id", None):
                last_text_channel_ids[ctx.guild.id] = ctx.channel.id
            if ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
                last_activity[ctx.guild.id] = time.time()
    except Exception:
        pass


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Los slash también cuentan como uso para evitar autodesconexión injusta."""
    try:
        if interaction.guild:
            if getattr(interaction.channel, "id", None):
                last_text_channel_ids[interaction.guild.id] = interaction.channel.id
            if interaction.guild.voice_client and interaction.guild.voice_client.is_connected():
                last_activity[interaction.guild.id] = time.time()
    except Exception:
        pass


@bot.event
async def on_command_error(ctx, error):
    """Manejo unificado de errores con sugerencias inteligentes"""
    # Ignorar comandos no encontrados que ya fueron manejados
    if isinstance(error, commands.CommandNotFound):
        return
    
    # Obtener el error original si es de invocación
    original_error = getattr(error, 'original', error)

    # Configuración de embeds para errores
    error_embeds = {
        commands.MissingPermissions: {
            "title": "🚫 Permisos insuficientes",
            "description": "No tienes los permisos necesarios para este comando.",
            "color": 0xff0000,
            "fields": [
                ("Permisos requeridos",
                 "\n".join(f"• `{perm.replace('_', ' ').title()}`" for perm in getattr(original_error, 'missing_permissions', [])))
            ]
        },
        commands.BotMissingPermissions: {
            "title": "🤖 Faltan permisos del bot",
            "description": "No tengo los permisos necesarios para ejecutar esto.",
            "color": 0xff0000,
            "fields": [
                ("Permisos faltantes",
                 "\n".join(f"• `{perm.replace('_', ' ').title()}`" for perm in getattr(original_error, 'missing_permissions', []))),
                ("Solución", "Por favor otórgame estos permisos o contacta a un administrador")
            ]
        },
        commands.CommandOnCooldown: {
            "title": "⏳ Comando en enfriamiento",
            "description": f"Debes esperar {original_error.retry_after:.1f} segundos para usar esto nuevamente.",
            "color": 0xf39c12,
            "fields": [
                ("Solución", "Espera un momento y vuelve a intentarlo.")
            ]
        },
        commands.BadArgument: {
            "title": "❌ Argumentos incorrectos",
            "description": "Has usado argumentos inválidos para este comando.",
            "color": 0xe74c3c,
            "fields": [
                ("Uso correcto", f"```{ctx.prefix}{ctx.command.name} {getattr(ctx.command, 'signature', '')}```"),
                ("Ejemplo",
                 f"```{getattr(ctx.command, 'brief', 'No hay ejemplo disponible')}```")
            ]
        },
        discord.NotFound: {
            "title": "🔍 Recurso no encontrado",
            "description": "El elemento solicitado no pudo ser encontrado.",
            "color": 0xe74c3c
        },
        discord.Forbidden: {
            "title": "🚫 Acceso prohibido",
            "description": "No tengo permisos para realizar esta acción.",
            "color": 0xe74c3c
        },
        discord.HTTPException: {
            "title": "🔌 Error de conexión",
            "description": "Hubo un problema al comunicarse con Discord.",
            "color": 0xe74c3c
        }
    }

    # Buscar el embed correspondiente al error
    embed = None
    for error_type, embed_config in error_embeds.items():
        if isinstance(original_error, error_type):
            embed = discord.Embed(
                title=embed_config["title"],
                description=embed_config["description"],
                color=embed_config["color"]
            )

            for field in embed_config.get("fields", []):
                if field[1]:  # Solo añadir si hay contenido
                    embed.add_field(name=field[0], value=field[1], inline=False)
            break

    # Si no encontramos un embed específico, usamos uno genérico
    if embed is None:
        logger.error(f"Error no manejado en comando {ctx.command}: {str(original_error)}", exc_info=True)
        embed = discord.Embed(
            title="💥 Error inesperado",
            description="Ocurrió un problema al ejecutar este comando.",
            color=0xe74c3c
        )
        error_msg = str(original_error)[:200]
        if error_msg:
            embed.add_field(
                name="Detalles técnicos",
                value=f"```{error_msg}```",
                inline=False
            )

    # Mostrar aliases si existen
    if hasattr(ctx.command, 'aliases') and ctx.command.aliases:
        embed.add_field(
            name="También puedes usar",
            value=", ".join(f"`{ctx.prefix}{alias}`" for alias in ctx.command.aliases),
            inline=False
        )

    try:
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"No se pudo enviar mensaje de error: {e}")


@bot.event
async def on_message(message):
    """Procesa comandos y convierte mensajes del canal #tickets en tickets privados."""
    if message.author.bot or not message.content:
        return

    try:
        ctx = await bot.get_context(message)

        if ctx.valid:
            await bot.invoke(ctx)
            return

        # En el canal público de tickets no se chatea: si alguien escribe, se crea ticket y se borra el mensaje.
        if message.guild and is_ticket_request_channel(message.channel):
            motivo = message.content.strip()
            try:
                await message.delete()
            except Exception:
                pass

            if isinstance(message.author, discord.Member) and motivo:
                ticket_channel = await create_private_ticket(message.guild, message.author, motivo, message.channel)
                if ticket_channel:
                    aviso = await message.channel.send(
                        f"{message.author.mention} ✅ ticket creado: {ticket_channel.mention}",
                        delete_after=10
                    )
                else:
                    await message.channel.send(
                        f"{message.author.mention} ⏳ espera unos segundos antes de abrir otro ticket.",
                        delete_after=8
                    )
            return

        await moderate_message(message)

    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}")
        try:
            if isinstance(e, discord.Forbidden):
                await message.channel.send("⚠️ No tengo permisos para realizar esta acción")
        except Exception:
            pass


@bot.event
async def on_message_edit(before, after):
    """Detección mejorada de ediciones"""
    if before.content != after.content and after.id not in bypass_messages:
        try:
            await moderate_message(after)
        except Exception as e:
            logger.error(f"Error moderando mensaje editado: {e}")


@bot.event
async def on_voice_state_update(member, before, after):
    """Maneja cambios de voz sin destruir cola ni música por desconexiones accidentales."""
    if member != bot.user:
        # Si entra/sale un humano, solo guardamos el canal.
        # No reiniciamos la actividad por presencia: si nadie usa el bot, debe poder bajarse solo.
        try:
            guild = member.guild
            vc = guild.voice_client
            if vc and vc.is_connected() and vc.channel:
                last_voice_channel_ids[guild.id] = vc.channel.id
                humans = [m for m in vc.channel.members if not m.bot]
                if not humans and not (vc.is_playing() or vc.is_paused()):
                    # El contador de "solo" empieza cuando se queda sin humanos.
                    last_activity[guild.id] = time.time()
        except Exception:
            pass
        return

    guild_id = before.channel.guild.id if before.channel else after.channel.guild.id

    if after.channel:
        last_voice_channel_ids[guild_id] = after.channel.id
        last_activity[guild_id] = time.time()
        return

    if before.channel and not after.channel:
        recent_attempt = voice_connection_attempts.get(guild_id, 0)
        if time.time() - recent_attempt < 35:
            logger.info(f"Salida de voz durante intento de conexión en guild {guild_id}; no limpio cola ni fuerzo reconexión.")
            return

        logger.warning(f"Bot salió del canal de voz en guild {guild_id}. Revisando si debe recuperar música...")

        karaoke_sessions = globals().get("KARAOKE_SESSIONS", {})
        karaoke_active = guild_id in karaoke_sessions and karaoke_sessions[guild_id].get("active", False)
        active_state = bool(
            current_songs.get(guild_id)
            or queues.get(guild_id)
            or dj_sessions.get(guild_id, {}).get("active", False)
            or karaoke_active
            or guild_id in voice_stay_connected_guilds
        )

        if active_state:
            last_voice_channel_ids[guild_id] = before.channel.id
            create_logged_task(recover_music_after_disconnect(before.channel.guild, before.channel), f"recover_music_voice_state_{guild_id}")
            return

        # Solo limpiamos cuando no hay nada activo.
        current_songs.pop(guild_id, None)
        last_activity.pop(guild_id, None)


if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN/discord_token. En Render crea la variable discord_token; localmente puedes usar .env con DISCORD_TOKEN.")


# Servidor web pequeño para Render/UptimeRobot.
# No cambia la lógica del bot; solo mantiene un endpoint HTTP activo.
try:
    from webserver import keepalive
    keepalive()
except Exception as e:
    logger.warning(f"No se pudo iniciar webserver keepalive: {e}")

bot.run(TOKEN)
