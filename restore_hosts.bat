@echo off
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
copy /Y "E:\code\hermes\focus\hosts_clean.txt" "C:\Windows\System32\drivers\etc\hosts"
ipconfig /flushdns >nul
echo Hosts restored.
pause
