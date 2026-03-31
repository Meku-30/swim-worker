@echo off
chcp 65001 >nul 2>&1
title SWIM Worker

echo ==========================================
echo   SWIM Worker
echo ==========================================
echo.

if not exist ".env" (
    echo [エラー] .env ファイルが見つかりません。
    echo .env.example をコピーして .env を作成し、設定を記入してください。
    echo.
    pause
    exit /b 1
)

if not exist "ca.crt" (
    echo [エラー] ca.crt ファイルが見つかりません。
    echo 管理者から ca.crt を入手して同じフォルダに配置してください。
    echo.
    pause
    exit /b 1
)

echo 起動中...
echo 停止するには Ctrl+C を押してください。
echo.

swim-worker-windows.exe

pause
