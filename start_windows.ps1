Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force
Set-Location $PSScriptRoot

$pyCmd = "py"
$pyArgs = @("-3")
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    $pyCmd = "python"
    $pyArgs = @()
}

if (!(Test-Path ".venv")) {
    Write-Host "Creando entorno virtual..."
    & $pyCmd @pyArgs -m venv .venv
}

. .\.venv\Scripts\Activate.ps1

Write-Host "Instalando/actualizando dependencias..."
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ADVERTENCIA: ffmpeg no esta en PATH." -ForegroundColor Yellow
    Write-Host "Instala con: winget install Gyan.FFmpeg"
    Write-Host "Luego cierra y abre PowerShell/VS Code."
}

if (!(Test-Path ".env")) {
    Write-Host "No existe .env. Copia .env.example como .env y pega tus llaves." -ForegroundColor Yellow
    exit 1
}

New-Item -ItemType Directory -Force -Path data, data\history, data\playlists | Out-Null

Write-Host "Iniciando Archeon..."
python bot.py
