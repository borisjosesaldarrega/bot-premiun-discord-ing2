# Archeon Bot - notas rápidas DZKnight

## Estado actual

Bot de Discord en Python con:

- Música con `FFmpegPCMAudio + PCMVolumeTransformer`.
- Volumen por servidor con `¡volumen` y `/volumen`.
- Modo DJ con memoria para evitar repetir canciones.
- Tickets privados.
- Bienvenida personalizada.
- Memes/ship con imágenes.
- Moderación y strikes.
- IA opcional con Gemini.
- Prefijo principal: `¡`
- Slash commands: `/`

## FFmpeg

Si sale `ffmpeg was not found`, instala:

```powershell
winget install Gyan.FFmpeg
```

Luego cierra y abre PowerShell/VS Code y prueba:

```powershell
ffmpeg -version
```

## Instalación Windows

```powershell
cd "C:\Users\salda\Downloads\ARCHEON DISCORD"
.\.venv\Scripts\activate
python -m pip install -U -r requirements.txt
python bot.py
```

## Instalación Linux/VPS

```bash
chmod +x start.sh
./start.sh
```

## Variables importantes del `.env`

```env
DISCORD_TOKEN=tu_token_nuevo_del_bot
AUTO_VOICE_DISCONNECT=true
VOICE_ALONE_TIMEOUT=180
VOICE_IDLE_TIMEOUT=600
DJ_USE_GEMINI=false
MODERATION_AI_ENABLED=false
```

`DISCORD_TOKEN` es obligatorio. Las llaves de Google/Stability son opcionales si no usarás IA o imágenes por API.
