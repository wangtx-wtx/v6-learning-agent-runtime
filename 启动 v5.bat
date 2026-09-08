@echo off
chcp 65001 >nul
title v5.1 Learning Agent Runtime
echo ============================================================
echo   v5.1 Learning Agent Runtime
echo ============================================================
echo.

REM === Start backend ===
echo Starting backend (port 8800)...
start "v5 backend" cmd /k "cd /d %~dp0backend && python run.py"

REM === Start frontend ===
echo Starting frontend (Vite port 5173)...
start "v5 frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

REM === Wait then open browser ===
echo.
echo Waiting for services to be ready...
timeout /t 5 /nobreak >nul

start "" http://localhost:5173

echo.
echo ============================================
echo   Backend:  http://127.0.0.1:8800
echo   Frontend: http://localhost:5173
echo   Gateway:  http://127.0.0.1:8080/admin
echo ============================================
echo.
pause