"""
Servidor web con Flask para mantener activo el bot de Discord (Archeon).
"""
import os
from datetime import datetime, timezone
from threading import Thread

from flask import Flask, jsonify

app = Flask(__name__)

# Guardamos la hora de inicio del servidor
STARTED_AT: str = datetime.now(timezone.utc).isoformat()


@app.route("/")
def home() -> str:
    """Ruta principal que muestra una interfaz web estilizada."""
    # Retornamos HTML con CSS embebido para que la web se vea moderna
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Archeon Bot</title>
        <style>
            body {
                background-color: #2c2f33; /* Color de fondo oscuro de Discord */
                color: #ffffff;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
            }
            .container {
                text-align: center;
                background-color: #23272a; /* Color de fondo de la tarjeta */
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 8px 16px rgba(0, 0, 0, 0.3);
            }
            h1 { 
                color: #5865F2; /* Azul característico de Discord */ 
                margin-bottom: 10px;
            }
            p { 
                font-size: 1.2em; 
                color: #b9bbbe;
            }
            a { 
                color: #57F287; /* Verde de éxito de Discord */
                text-decoration: none; 
                font-weight: bold; 
                padding: 10px 15px;
                border: 1px solid #57F287;
                border-radius: 5px;
                display: inline-block;
                margin-top: 20px;
                transition: all 0.3s;
            }
            a:hover { 
                background-color: #57F287; 
                color: #23272a;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>✅ Archeon está despierto</h1>
            <p>El servidor Flask está manteniendo vivo al bot correctamente.</p>
            <a href="/health">Revisar estado interno (/health)</a>
        </div>
    </body>
    </html>
    """


@app.route("/health")
def health():
    """Ruta de salud para verificar el estado y tiempo de actividad del bot."""
    return jsonify({
        "status": "online",
        "service": "Archeon Discord Bot",
        "started_at": STARTED_AT,
    })


def run() -> None:
    """Inicia el servidor Flask en el puerto configurado."""
    # Render entrega el puerto en la variable PORT. En local usa 8080.
    port: int = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def keepalive() -> Thread:
    """Inicia Flask en un hilo secundario para que el bot siga corriendo."""
    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread
