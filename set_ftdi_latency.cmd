@echo off
REM ============================================================
REM  One-click: set FTDI USB-serial LatencyTimer to 1 ms (default 16 ms).
REM  The FlexiTac reading board (Arduino Nano w/ FT232R) is an FTDI device.
REM  Needs admin -> this script re-launches itself with a UAC prompt.
REM  Logic lives in set_ftdi_latency.ps1 next to this file.
REM ============================================================
cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights ^(UAC prompt^)...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set_ftdi_latency.ps1"
echo.
echo Press any key to close.
pause >nul
