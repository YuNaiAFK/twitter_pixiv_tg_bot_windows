@echo off
cd /d "%~dp0"
if not exist .env (
  echo Missing .env. Copy .env.example to .env and configure BOT_TOKEN.
  pause
  exit /b 1
)

REM ---------------------------------------------------------------------------
REM Self-hosted Bot API server.
REM
REM Started here, before the bot, because without it the bot silently falls back
REM to the public API and the upload limit drops from 2000 MB to 50 MB - and the
REM failure does not say so. run_local_api.py --ensure brings up whatever is
REM missing (Docker Desktop, the host relay, the container) and waits until the
REM server answers. It does nothing when everything is already running, so this
REM is safe on every start.
REM
REM A failure here is not fatal: the bot still runs against the public API, just
REM with the smaller limit, so there is no point blocking the start.
REM ---------------------------------------------------------------------------
if not "%TELEGRAM_SKIP_LOCAL_API%"=="1" (
  if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe run_local_api.py --ensure
  ) else (
    py run_local_api.py --ensure
  )
  if errorlevel 1 (
    echo.
    echo   [warn] Self-hosted Bot API server is not ready.
    echo          The bot will use the public API: 50 MB upload limit instead of 2000 MB.
    echo          Run this to see why:  python run_local_api.py --check
    echo.
  )
)

if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe bot.py
) else (
  py bot.py
)
pause
