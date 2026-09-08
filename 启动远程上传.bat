@echo off
chcp 65001 >nul
title v5 远程上传 (Tailscale Serve)
cd /d "%~dp0"

echo ================================================
echo   v5 远程上传模式 (Tailscale Serve)
echo ================================================
echo.

REM --- 1. 检查 Tailscale ---
where tailscale >nul 2>&1
if errorlevel 1 (
  echo [X] Tailscale 未安装。请先从 https://tailscale.com/download 安装并登录。
  pause
  exit /b 1
)

REM --- 2. 检查在线状态 ---
tailscale status >nul 2>&1
if errorlevel 1 (
  echo [!] Tailscale 未登录,尝试自动登录...
  tailscale up
  if errorlevel 1 (
    echo [X] Tailscale 登录失败。请手动执行 tailscale up 完成认证。
    pause
    exit /b 1
  )
)

REM --- 3. 配置 Serve ---
echo [1/3] 配置 tailscale serve --bg 8800 ...
tailscale serve --bg 8800 >nul 2>&1
if errorlevel 1 (
  echo [!] tailscale serve 配置失败,继续启动后端但远程 URL 可能未生效。
)
echo.

REM --- 4. 提取 ts.net URL ---
for /f "delims=" %%i in ('powershell -NoProfile -Command "tailscale status --json | %%{ $$j = $$_ | ConvertFrom-Json; $$j.Self.DNSName }"') do set TS_DNS=%%i
if defined TS_DNS (
  REM 去掉尾部的点
  set TS_DNS=%TS_DNS:.=%
  REM 实际上尾巴的点有意义,我们保留原 DNS
  for /f "delims=" %%j in ('powershell -NoProfile -Command "tailscale status --json | %%{ ($$_ | ConvertFrom-Json).Self.DNSName }"') do set TS_DNS=%%j
)

echo ================================================
echo   手机浏览器访问(Tailscale 内网穿透):
if defined TS_DNS (
  echo   https://%TS_DNS%/#/m-upload
) else (
  echo   (未能自动获取 DNS 名,请运行 tailscale status 查看)
  echo   常见形式: https://<computer-name>.<tailnet>.ts.net/#/m-upload
)
echo.
echo   要求:手机已安装 Tailscale App,且登录同一账号。
echo ================================================
echo.

REM --- 5. 启动后端 ---
echo [2/3] 启动 v5 后端 ...
cd /d "%~dp0backend"
python run.py
goto :eof