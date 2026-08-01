@echo off
title Study With Focus - Backend

:: Auto-elevate if not admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
set "FDIR=%~dp0"
set "FDIR=%FDIR:\=/%"

:: Kill old backend if running
taskkill /f /im pythonw.exe >nul 2>&1

echo ================================
echo   Study With Focus Backend
echo   file:///%FDIR%index.html
echo ================================
echo Starting backend on port 8765...
pythonw server.py

:: Wait for server to be ready
echo Waiting for server...
:wait
timeout /t 1 /nobreak >nul
curl -s http://localhost:8765/api/status >nul 2>&1
if errorlevel 1 goto wait

:: Open browser
echo Opening...
start "" "file:///%FDIR%index.html"
exit
