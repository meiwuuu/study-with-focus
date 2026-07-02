@echo off
title Study With Focus
cd /d "%~dp0"

:: Elevate to admin via VBScript (faster than PowerShell)
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "cmd.exe", "/c ""%~s0""", "", "runas", 1 >> "%temp%\getadmin.vbs"
    cscript //nologo "%temp%\getadmin.vbs"
    del "%temp%\getadmin.vbs"
    exit /b
)

:: Precision kill: only kill the process on port 8765
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8765" ^| find "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)

:: Start backend; server opens browser when ready
start "" pythonw server.py --launch-browser
exit
