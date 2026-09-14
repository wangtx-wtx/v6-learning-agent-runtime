@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title V6.0 Learning Agent Runtime
echo ============================================================
echo   V6.0 Learning Agent Runtime - Production
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  pause
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js not found in PATH.
  pause
  exit /b 1
)

set "V5_ENV=production"
set "V5_GATEWAY_MODE=live"
set "V6_LEARNING_ENGINE=on"
set "V6_SEGMENT_GATEWAY_TIMEOUT=600"
set "V5_DATA_ROOT="
set "V5_GATEWAY_URL=http://127.0.0.1:8317"
set "V5_GATEWAY_HOME=C:\Users\28595\Desktop\新建文件夹 (5)\local-llm-gateway-v1.0.3-windows-x64-portable"
if /i "%~1"=="fake" (
  set "V5_ENV=development"
  set "V5_GATEWAY_MODE=fake"
  set "V5_DATA_ROOT=%~dp0demo_data"
)

if /i "%~1"=="check" (
  if not exist "%~dp0frontend\node_modules\vite\bin\vite.js" (
    where npm >nul 2>nul
    if errorlevel 1 (
      echo [ERROR] Frontend dependencies are missing and npm is unavailable.
      exit /b 1
    )
  )
  echo [OK] Python, Node.js and frontend startup dependencies are available.
  exit /b 0
)

if /i "!V5_GATEWAY_MODE!"=="live" (
  powershell -NoProfile -Command "exit -not (Test-NetConnection 127.0.0.1 -Port 8317 -InformationLevel Quiet)" >nul 2>nul
  if errorlevel 1 (
    if exist "!V5_GATEWAY_HOME!\Start-Gateway.ps1" (
      echo [INFO] Starting Local LLM Gateway on port 8317...
      powershell -NoProfile -ExecutionPolicy Bypass -File "!V5_GATEWAY_HOME!\Start-Gateway.ps1" -NoBrowser
    ) else (
      echo [WARN] Local LLM Gateway port 8317 is not listening.
      echo [WARN] Gateway launcher was not found: !V5_GATEWAY_HOME!
      echo [HINT] For zero-token demo use: 启动 v5.bat fake
      echo.
    )
  )
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm is required for the production frontend build.
  pause
  exit /b 1
)
echo Building production frontend...
pushd "%~dp0frontend"
call npm run build
if errorlevel 1 (
  popd
  echo [ERROR] Frontend build failed. Services were not started.
  pause
  exit /b 1
)
popd

echo Starting backend (port 8800, gateway=%V5_GATEWAY_MODE%)...
start "v5 backend" cmd /k "cd /d %~dp0backend && set V5_ENV=%V5_ENV%&& set V5_GATEWAY_MODE=%V5_GATEWAY_MODE%&& set V6_LEARNING_ENGINE=%V6_LEARNING_ENGINE%&& set V6_SEGMENT_GATEWAY_TIMEOUT=%V6_SEGMENT_GATEWAY_TIMEOUT%&& set V5_GATEWAY_URL=%V5_GATEWAY_URL%&& set V5_DATA_ROOT=%V5_DATA_ROOT%&& python run.py"

echo Starting production frontend preview (port 5173)...
where npm >nul 2>nul
if not errorlevel 1 (
  start "V6 frontend" cmd /k "cd /d %~dp0frontend && npm run preview -- --host 127.0.0.1 --port 5173"
) else (
  if exist "%~dp0frontend\node_modules\vite\bin\vite.js" (
    echo [INFO] npm not found; using installed Vite through node.
    start "v5 frontend" cmd /k "cd /d %~dp0frontend && node node_modules\vite\bin\vite.js"
  ) else (
    echo [ERROR] npm not found and frontend dependencies are not installed.
    echo Install a complete Node.js distribution with npm, then run npm install.
    pause
    exit /b 1
  )
)

echo.
echo Waiting for services to be ready...
timeout /t 5 /nobreak >nul
start "" http://127.0.0.1:8800/

echo.
echo ============================================
echo   App:      http://127.0.0.1:8800/
echo   Preview:  http://127.0.0.1:5173
echo   Gateway:  http://127.0.0.1:8317/admin [%V5_GATEWAY_MODE%]
echo ============================================
echo.
pause
endlocal
