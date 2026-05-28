# Archeon Bot - paquete corregido para hosting

Este paquete contiene los archivos auxiliares corregidos para ejecutar el bot en Windows o en un VPS Linux.

## Archivos principales

- `requirements.txt`: dependencias del bot en UTF-8.
- `.env.example`: plantilla segura de variables.
- `start_windows.ps1`: inicio recomendado en Windows.
- `start_windows.bat`: inicio alternativo en Windows.
- `instalar_dependencias_windows.ps1`: instala dependencias y muestra versiones.
- `start.sh`: inicio recomendado en Linux/VPS.
- `.gitignore`: evita subir `.env`, `.venv`, logs, cookies y archivos privados.
- `cookies.txt.example`: plantilla opcional para yt-dlp.

## Requisitos externos

FFmpeg no se instala con pip. Debe instalarse en el sistema.

### Windows

```powershell
winget install Gyan.FFmpeg
```

Cierra y abre PowerShell/VS Code, luego prueba:

```powershell
ffmpeg -version
```

### Ubuntu/Debian VPS

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg unzip git
```

## Instalación rápida en Windows

```powershell
cd "C:\Users\salda\Downloads\ARCHEON DISCORD"
copy .env.example .env
notepad .env
.\instalar_dependencias_windows.ps1
.\start_windows.ps1
```

## Instalación rápida en Linux/VPS

```bash
chmod +x start.sh
cp .env.example .env
nano .env
./start.sh
```

## Mantenerlo 24/7 en Linux con systemd

Crea el servicio:

```bash
sudo nano /etc/systemd/system/archeon.service
```

Ejemplo:

```ini
[Unit]
Description=Archeon Discord Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/archeon
ExecStart=/home/ubuntu/archeon/.venv/bin/python /home/ubuntu/archeon/bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Activa:

```bash
sudo systemctl daemon-reload
sudo systemctl enable archeon
sudo systemctl start archeon
journalctl -u archeon -f
```

## Importante

Nunca subas ni compartas:

- `.env`
- `cookies.txt`
- `.venv/`
- `bot.log`
- tokens/API keys

Si alguna llave se compartió por error, regénérala antes de alojar el bot.
