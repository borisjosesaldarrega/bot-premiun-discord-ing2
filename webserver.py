import os
from datetime import datetime, timezone
from threading import Thread

from flask import Flask, jsonify

app = Flask(__name__)

STARTED_AT = datetime.now(timezone.utc).isoformat()


@app.route("/")
def home():
    return (
        "✅ Archeon está despierto. "
        "Usa /health para revisar estado."
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "online",
        "service": "Archeon Discord Bot",
        "started_at": STARTED_AT,
    })


def run():
    # Render entrega el puerto en la variable PORT.
    # En local usa 8080.
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def keepalive():
    """Inicia Flask en un hilo secundario para que el bot siga corriendo."""
    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread
