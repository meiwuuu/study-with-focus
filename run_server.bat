@echo off
title Study With Focus - Backend

:: Auto-elevate if not admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "E:\code\hermes\focus"

:: Kill old backend if running
taskkill /f /im pythonw.exe >nul 2>&1

echo ================================
echo   Study With Focus Backend
echo   http://localhost:8765
echo ================================
echo.
python server.py
pause
