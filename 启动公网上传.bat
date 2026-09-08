@echo off
chcp 65001 >nul
title v5 公网上传 (Tailscale Funnel + Token)
cd /d "%~dp0"

echo ================================================
echo   v5 公网上传模式 (Tailscale Funnel + Token)
echo ================================================
echo.
echo [!] Funnel 会通过 Tailscale 公网边缘节点暴露后端,
echo     任何互联网设备无需 Tailscale 也可访问。必须配 Token 鉴权。
echo.

REM --- 1. 检查 .env 是否有 Token ---
set TOKEN_FOUND=
if exist "%~dp0backend\.env" (
  for /f "tokens=1,* delims==" %%a in ('findstr /B "V5_MOBILE_TOKEN=" "%~dp0backend\.env"') do (
    if /i "%%a"=="V5_MOBILE_TOKEN" if not "%%b"=="" set TOKEN_FOUND=1
  )
)
if not defined TOKEN_FOUND (
  if not defined V5_MOBILE_TOKEN (
    echo [X] backend/.env 中未找到 V5_MOBILE_TOKEN。
    echo     请先在 backend/.env 添加:V5_MOBILE_TOKEN=你的密码
    echo.
    pause
    exit /b 1
  )
)
echo [1/4] V5_MOBILE_TOKEN 已配置。

REM --- 2. 检查 Tailscale 在线 ---
where tailscale >nul 2>&1
if errorlevel 1 (
  echo [X] Tailscale 未安装。
  pause
  exit /b 1
)
tailscale status >nul 2>&1
if errorlevel 1 (
  echo [!] Tailscale 未登录,自动 up ...
  tailscale up || (echo [X] 登录失败 & pause & exit /b 1)
)
echo [2/4] Tailscale 在线。

REM --- 3. 启用 Funnel ---
echo [3/4] 配置 tailscale funnel --bg 8800 ...
tailscale funnel --bg 8800 >nul 2>&1
if errorlevel 1 (
  echo [X] Funnel 配置失败。检查 Tailscale 账号是否启用了 Funnel 权限。
  echo     访问 https://login.tailscale.com/admin/features 启用 HTTPS / Funnel。
  pause
  exit /b 1
)

REM --- 4. 提取 URL ---
for /f "delims=" %%i in ('powershell -NoProfile -Command "(tailscale status --json | ConvertFrom-Json).Self.DNSName"') do set TS_DNS=%%i
for /f "delims=" %%k in ('powershell -NoProfile -Command "(Get-Content backend\.env | Where-Object { $_ -match '^V5_MOBILE_TOKEN=' }) -replace '^V5_MOBILE_TOKEN=','' -replace '[\"\'']',''"') do set TOKEN=%%k

echo ================================================
echo   公网访问地址(任何人可访问):
if defined TS_DNS (
  echo   https://%TS_DNS%/#/m-upload?token=%TOKEN%
) else (
  echo   (获取 DNS 失败,请运行 tailscale status)
)
echo.
echo   请把以上 URL 和 token 发给需要使用的人。
echo ================================================
echo.

REM --- 5. 启动后端 ---
echo [4/4] 启动 v5 后端 ...
cd /d "%~dp0backend"
python run.py
goto :eof