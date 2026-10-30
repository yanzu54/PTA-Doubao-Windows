@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
title PTA Doubao - One Click - Setup Fix
echo ================================================
echo PTA - Doubao - Windows One Click (Setup Fix)
echo ================================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_and_run.ps1"
set "RC=%ERRORLEVEL%"
echo.
echo Finished. Exit code: %RC%
echo If setup failed, send run_logs\setup_diagnostics.log
pause
exit /b %RC%
