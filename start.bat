@echo off
title Study With Focus

:: Auto-elevate if not admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "E:\code\hermes\focus"

:: Start backend
echo ================================
echo   Study With Focus
echo ================================
echo Starting backend on port 8765...
start "" pythonw server.py

:: Wait for server to be ready
echo Waiting for server...
:wait
timeout /t 1 /nobreak >nul
curl -s http://localhost:8765/api/status >nul 2>&1
if errorlevel 1 goto wait

:: Open browser
echo Opening...
start "" "file:///E:/code/hermes/focus/index.html"
exit
