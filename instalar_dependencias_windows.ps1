Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force
Set-Location $PSScriptRoot

if (!(Test-Path ".venv")) {
    Write-Host "Creando entorno virtual..."
    py -3 -m venv .venv
}

. .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

Write-Host ""
Write-Host "Versiones instaladas importantes:" -ForegroundColor Cyan
python -m pip show discord.py aiohttp yt-dlp python-dotenv deep-translator google-generativeai PyNaCl davey Pillow audioop-lts

Write-Host ""
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ADVERTENCIA: ffmpeg no existe en PATH." -ForegroundColor Yellow
    Write-Host "Instala con: winget install Gyan.FFmpeg"
} else {
    ffmpeg -version | Select-Object -First 1
}

Write-Host ""
Write-Host "Listo. Para iniciar: .\start_windows.ps1" -ForegroundColor Green
