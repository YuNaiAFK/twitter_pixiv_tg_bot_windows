@echo off
cd /d "%~dp0"
if not exist .env (
  echo Missing .env. Copy .env.example to .env and configure BOT_TOKEN.
  pause
  exit /b 1
)
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe bot.py
) else (
  py bot.py
)
pause
