@echo off
chcp 65001 >nul 2>&1
title SWIM Worker

echo ==========================================
echo   SWIM Worker
echo ==========================================
echo.

if not exist "ca.crt" (
    echo [エラー] ca.crt ファイルが見つかりません。
    echo 管理者から ca.crt を入手して同じフォルダに配置してください。
    echo.
    pause
    exit /b 1
)

if exist "swim-worker-gui-windows.exe" (
    start "" swim-worker-gui-windows.exe
) else if exist "swim-worker-windows.exe" (
    swim-worker-windows.exe
) else (
    echo [エラー] 実行ファイルが見つかりません。
    pause
    exit /b 1
)
