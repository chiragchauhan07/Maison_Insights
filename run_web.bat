@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\activate.bat" (
  echo Create the virtual environment first: python -m venv venv
  pause
  exit /b 1
)
start "Maison Insights" /D "%~dp0" cmd /k "call venv\Scripts\activate.bat && python -m uvicorn server:app --host 127.0.0.1 --port 8000"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8000"
endlocal
