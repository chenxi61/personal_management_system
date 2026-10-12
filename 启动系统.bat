@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0启动助手.ps1"
if errorlevel 1 (
    echo 启动失败，请保留上面的错误信息。
    pause
)
