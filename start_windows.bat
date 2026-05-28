@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set PY_CMD=py -3
) else (
    set PY_CMD=python
)

if not exist ".venv" (
    echo Creando entorno virtual...
    %PY_CMD% -m venv .venv
)

call ".venv\Scripts\activate.bat"

echo Instalando/actualizando dependencias...
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

where ffmpeg >nul 2>nul
if not %errorlevel%==0 (
    echo ADVERTENCIA: ffmpeg no esta en PATH.
    echo Instala con: winget install Gyan.FFmpeg
    echo Luego cierra y abre la terminal.
)

if not exist ".env" (
    echo No existe .env. Copia .env.example como .env y pega tus llaves.
    pause
    exit /b 1
)

if not exist "data" mkdir data
if not exist "data\history" mkdir data\history
if not exist "data\playlists" mkdir data\playlists

echo Iniciando Archeon...
python bot.py
pause
