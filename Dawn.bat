@echo off
REM Dawn — 더블클릭 실행 (Windows). 첫 실행 시 환경 자동 설치.
cd /d "%~dp0"

if exist "dist\Dawn.exe" (
  start "" "dist\Dawn.exe"
  exit /b 0
)

if not exist ".venv\Scripts\pythonw.exe" (
  echo First run: setting up Python environment... ^(1-2 min^)
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Python3 required: https://python.org
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install -q --upgrade pip
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt || (
    echo Setup failed - check internet connection.
    pause
    exit /b 1
  )
)

REM deno: improves YouTube reliability (JS challenge). Optional — app works without it.
where deno >nul 2>nul
if errorlevel 1 (
  where winget >nul 2>nul && winget install -e --id DenoLand.Deno --silent --accept-package-agreements --accept-source-agreements >nul 2>nul
  set "PATH=%USERPROFILE%\.deno\bin;%PATH%"
)

start "" ".venv\Scripts\pythonw.exe" main.py
