# Archeon Bot - música, DJ y roasts

## Música

- La búsqueda musical prueba YouTube y SoundCloud como respaldo.
- Si YouTube bloquea con `Sign in to confirm you're not a bot`, usa enlace directo o `cookies.txt`.
- El bot necesita FFmpeg instalado en el sistema.

## Modo DJ

- `¡dj tema o artista`
- `¡dj stop`

Configuración recomendada:

```env
DJ_USE_GEMINI=false
DJ_RECENT_MEMORY=30
```

Así el DJ no depende de cuota de Gemini y evita repetir canciones como disco rayado.

## Roasts / insultos de broma

- `¡insultar @usuario razón`
- `/insultar`

El comando menciona al usuario una sola vez y evita duplicar el nombre después del `@`.

## Instalación recomendada

```powershell
python -m pip install -U pip setuptools wheel
python -m pip install -r requirements.txt
```
