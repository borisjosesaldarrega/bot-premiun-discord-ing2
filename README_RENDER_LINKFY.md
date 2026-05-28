# Archeon Bot — paquete para Render + UptimeRobot

Este ZIP está preparado según el flujo del video que describiste, pero aplicado a tu bot Archeon.

## Qué se cambió sin tocar la lógica del bot

- Se agregó `webserver.py` con Flask para tener una URL activa.
- `bot.py` ahora acepta el token desde `discord_token` **o** `DISCORD_TOKEN`.
- Antes de `bot.run(TOKEN)` se llama `keepalive()` para levantar el servidor web.
- Se agregó `Flask` a `requirements.txt`.
- Se quitaron del ZIP: `.env`, `.venv`, `bot.log`, `cookies.txt` real y cachés.

## Estructura importante

```txt
bot.py
webserver.py
requirements.txt
.env.example
Procfile
render.yaml
runtime.txt
start.sh
```

## Probar local en PowerShell

```powershell
cd "C:\Users\salda\Downloads\ARCHEON DISCORD"
python -m pip install -r requirements.txt
$env:discord_token="TU_TOKEN_NUEVO"
python bot.py
```

Abre en navegador:

```txt
http://localhost:8080
http://localhost:8080/health
```

## Subir a GitHub

1. Crea repo privado.
2. Sube estos archivos, pero NO subas `.env`.
3. Confirma que `.venv/` no está en el repo.

## Render

Crear `New Web Service` desde GitHub.

Configura:

```txt
Build Command: pip install -r requirements.txt
Start Command: python bot.py
```

Environment Variables:

```env
discord_token=TU_TOKEN_NUEVO
GOOGLE_API_KEY=
STABILITY_API_KEY=
AUTO_VOICE_DISCONNECT=true
MODERATION_AI_ENABLED=false
DJ_USE_GEMINI=false
```

Si usas tickets/bienvenida, agrega también:

```env
TICKET_REQUEST_CHANNEL_ID=1387966992811556944
TICKET_LOG_CHANNEL_ID=1387966992811556944
TICKET_STAFF_ROLE_IDS=1509364337259581440
WELCOME_CHANNEL_ID=902057453204697092
```

## UptimeRobot

Cuando Render te dé una URL, crea un monitor HTTP cada 5 minutos apuntando a:

```txt
https://TU-SERVICIO.onrender.com/health
```

## Importante

Render puede pedir tarjeta según región/cuenta. Si te pide tarjeta y no quieres, usa un hosting de bots con Docker/Python o busca cupo en otro servicio gratuito.

Regenera tu token de Discord antes de alojarlo si alguna vez lo pegaste o compartiste.
