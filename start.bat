@echo off
cd /d "%~dp0"
start "Reel Transcriber Server" /min "venv\Scripts\python.exe" app.py
timeout /t 3 /nobreak >nul
start "" "http://localhost:5151"
