@echo off
rem refind-bless launcher for Windows: self-elevates, then starts the app
rem without a console window.
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
  exit /b
)
cd /d "%~dp0"
where pyw >nul 2>&1
if not errorlevel 1 (
  start "" pyw -3 refind_bless.py
) else (
  start "" pythonw refind_bless.py
)
