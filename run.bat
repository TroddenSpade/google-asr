@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

".venv\Scripts\python.exe" app.py %*
exit /b %errorlevel%

:error
echo.
echo Setup failed. Make sure Python 3.10+ is installed and available on PATH.
pause
exit /b 1
