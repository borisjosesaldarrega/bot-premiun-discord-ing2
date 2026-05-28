"""
Servidor web con Flask para mantener activo el bot de Discord (Archeon).
Pensado para Render + UptimeRobot.
"""
import os
from datetime import datetime, timezone
from threading import Thread

from flask import Flask, jsonify

app = Flask(__name__)

STARTED_AT = datetime.now(timezone.utc)


def format_uptime() -> str:
    """Devuelve el tiempo activo en formato legible."""
    delta = datetime.now(timezone.utc) - STARTED_AT
    total_seconds = int(delta.total_seconds())

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")

    return " ".join(parts)


@app.route("/")
def home() -> str:
    """Página principal visual del keepalive."""
    uptime = format_uptime()
    started_at = STARTED_AT.strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Archeon Bot | Online</title>
        <style>
            :root {{
                --bg: #070b16;
                --card: rgba(13, 18, 32, 0.82);
                --card-border: rgba(0, 255, 255, 0.22);
                --primary: #18f0ff;
                --secondary: #7c3cff;
                --success: #57f287;
                --text: #ffffff;
                --muted: #a7b0c3;
                --danger: #ff4d6d;
            }}

            * {{
                box-sizing: border-box;
            }}

            body {{
                margin: 0;
                min-height: 100vh;
                font-family: Inter, "Segoe UI", Arial, sans-serif;
                color: var(--text);
                background:
                    radial-gradient(circle at 20% 20%, rgba(24, 240, 255, 0.18), transparent 28%),
                    radial-gradient(circle at 80% 0%, rgba(124, 60, 255, 0.20), transparent 32%),
                    radial-gradient(circle at 50% 100%, rgba(87, 242, 135, 0.10), transparent 28%),
                    var(--bg);
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 24px;
                overflow: hidden;
            }}

            body::before {{
                content: "";
                position: fixed;
                inset: 0;
                background-image:
                    linear-gradient(rgba(255,255,255,0.035) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(255,255,255,0.035) 1px, transparent 1px);
                background-size: 42px 42px;
                mask-image: radial-gradient(circle, black 35%, transparent 80%);
                pointer-events: none;
            }}

            .shell {{
                width: min(960px, 100%);
                position: relative;
                z-index: 1;
            }}

            .card {{
                background: var(--card);
                border: 1px solid var(--card-border);
                border-radius: 28px;
                padding: 34px;
                box-shadow:
                    0 30px 90px rgba(0, 0, 0, 0.45),
                    0 0 60px rgba(24, 240, 255, 0.10);
                backdrop-filter: blur(16px);
            }}

            .top {{
                display: flex;
                align-items: center;
                gap: 18px;
                margin-bottom: 26px;
            }}

            .logo {{
                width: 72px;
                height: 72px;
                border-radius: 50%;
                display: grid;
                place-items: center;
                background:
                    radial-gradient(circle at 35% 25%, rgba(24, 240, 255, 0.35), transparent 38%),
                    #020611;
                border: 1px solid rgba(24, 240, 255, 0.35);
                box-shadow: 0 0 30px rgba(24, 240, 255, 0.28);
                font-size: 34px;
            }}

            .badge {{
                display: inline-flex;
                align-items: center;
                gap: 8px;
                color: var(--success);
                background: rgba(87, 242, 135, 0.10);
                border: 1px solid rgba(87, 242, 135, 0.28);
                padding: 8px 12px;
                border-radius: 999px;
                font-size: 14px;
                font-weight: 700;
                letter-spacing: 0.2px;
            }}

            .dot {{
                width: 9px;
                height: 9px;
                border-radius: 50%;
                background: var(--success);
                box-shadow: 0 0 18px var(--success);
                animation: pulse 1.8s infinite;
            }}

            @keyframes pulse {{
                0%, 100% {{ transform: scale(1); opacity: 1; }}
                50% {{ transform: scale(1.45); opacity: 0.55; }}
            }}

            h1 {{
                margin: 10px 0 8px;
                font-size: clamp(34px, 6vw, 64px);
                line-height: 0.95;
                letter-spacing: -2px;
            }}

            .gradient {{
                background: linear-gradient(90deg, var(--primary), var(--success), var(--secondary));
                -webkit-background-clip: text;
                background-clip: text;
                color: transparent;
            }}

            .subtitle {{
                color: var(--muted);
                font-size: 18px;
                line-height: 1.65;
                max-width: 720px;
                margin: 0 0 28px;
            }}

            .stats {{
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 14px;
                margin: 28px 0;
            }}

            .stat {{
                padding: 18px;
                border-radius: 18px;
                background: rgba(255, 255, 255, 0.045);
                border: 1px solid rgba(255, 255, 255, 0.08);
            }}

            .stat span {{
                display: block;
                color: var(--muted);
                font-size: 13px;
                margin-bottom: 8px;
            }}

            .stat strong {{
                font-size: 18px;
            }}

            .actions {{
                display: flex;
                flex-wrap: wrap;
                gap: 12px;
                margin-top: 26px;
            }}

            .button {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                padding: 13px 18px;
                border-radius: 14px;
                text-decoration: none;
                font-weight: 800;
                transition: 0.2s ease;
            }}

            .button.primary {{
                color: #001014;
                background: linear-gradient(90deg, var(--primary), var(--success));
                box-shadow: 0 12px 28px rgba(24, 240, 255, 0.20);
            }}

            .button.secondary {{
                color: var(--text);
                border: 1px solid rgba(255, 255, 255, 0.12);
                background: rgba(255, 255, 255, 0.055);
            }}

            .button:hover {{
                transform: translateY(-2px);
                filter: brightness(1.07);
            }}

            .footer {{
                margin-top: 24px;
                color: rgba(255,255,255,0.45);
                font-size: 13px;
            }}

            @media (max-width: 720px) {{
                .card {{
                    padding: 24px;
                    border-radius: 22px;
                }}

                .top {{
                    align-items: flex-start;
                    flex-direction: column;
                }}

                .stats {{
                    grid-template-columns: 1fr;
                }}

                .button {{
                    width: 100%;
                }}
            }}
        </style>
    </head>
    <body>
        <main class="shell">
            <section class="card">
                <div class="top">
                    <div class="logo">⚡</div>
                    <div>
                        <div class="badge">
                            <span class="dot"></span>
                            Sistema online
                        </div>
                        <h1>Archeon está <span class="gradient">despierto</span></h1>
                    </div>
                </div>

                <p class="subtitle">
                    El servidor keepalive está activo y manteniendo vivo al bot de Discord.
                    Si estás viendo esto, Render está respondiendo correctamente.
                </p>

                <div class="stats">
                    <div class="stat">
                        <span>Estado</span>
                        <strong>✅ Online</strong>
                    </div>
                    <div class="stat">
                        <span>Uptime</span>
                        <strong>{uptime}</strong>
                    </div>
                    <div class="stat">
                        <span>Iniciado</span>
                        <strong>{started_at}</strong>
                    </div>
                </div>

                <div class="actions">
                    <a class="button primary" href="/health">Ver healthcheck</a>
                    <a class="button secondary" href="https://discord.com/" target="_blank" rel="noopener noreferrer">
                        Abrir Discord
                    </a>
                </div>

                <div class="footer">
                    Archeon Bot • Keepalive Flask • Render / UptimeRobot listo
                </div>
            </section>
        </main>
    </body>
    </html>
    """


@app.route("/health")
def health():
    """Ruta de salud para UptimeRobot y Render."""
    return jsonify({
        "status": "online",
        "service": "Archeon Discord Bot",
        "started_at": STARTED_AT.isoformat(),
        "uptime": format_uptime(),
        "port": int(os.environ.get("PORT", "8080")),
    })


def run() -> None:
    """Inicia el servidor Flask en el puerto configurado."""
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def keepalive() -> Thread:
    """Inicia Flask en un hilo secundario para que el bot siga corriendo."""
    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread
