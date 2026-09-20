# Set the FTDI USB-serial "Latency Timer" to 1 ms for every FTDI device Windows has ever seen.
#
# Why: the FlexiTac reading board (Arduino Nano, FT232R USB chip) shows up as
# "USB Serial Port (COMx)". The FTDI driver defaults LatencyTimer = 16 ms, which is the
# same knob as Device Manager -> COMx -> Properties -> Port Settings -> Advanced -> Latency Timer.
# 1 ms bounds the worst-case USB delivery delay of a partially filled 62-byte packet.
#
# Needs Administrator (writes HKLM). Double-click set_ftdi_latency.cmd - it self-elevates.
# Takes effect the next time the board is plugged in.

$ErrorActionPreference = 'Stop'
$target = 1

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not running as Administrator. Use set_ftdi_latency.cmd (it asks for UAC)." -ForegroundColor Red
    exit 2
}

$root = 'HKLM:\SYSTEM\CurrentControlSet\Enum\FTDIBUS'
if (-not (Test-Path $root)) {
    Write-Host "No FTDI device has ever been enumerated on this PC (no $root)." -ForegroundColor Yellow
    Write-Host "Plug the FlexiTac board in once, then re-run this script."
    exit 1
}

$keys = Get-ChildItem -Path $root -Recurse | Where-Object { $_.PSChildName -eq 'Device Parameters' }
if (-not $keys) {
    Write-Host "FTDIBUS exists but no 'Device Parameters' keys found." -ForegroundColor Yellow
    exit 1
}

$changed = 0
foreach ($k in $keys) {
    $props = Get-ItemProperty -Path $k.PSPath
    $port  = $props.PortName
    $cur   = $props.LatencyTimer
    Set-ItemProperty -Path $k.PSPath -Name 'LatencyTimer' -Value $target -Type DWord
    $now = (Get-ItemProperty -Path $k.PSPath).LatencyTimer
    Write-Host ("{0,-6}  LatencyTimer {1} -> {2}   [{3}]" -f $port, $cur, $now, ($k.PSPath -replace '^.*FTDIBUS\\', ''))
    if ($now -eq $target) { $changed++ }
}

Write-Host ""
Write-Host ("Done. {0} FTDI device(s) now at LatencyTimer = {1} ms." -f $changed, $target) -ForegroundColor Green
Write-Host "Re-plug the FlexiTac USB cable for the new value to apply."
exit 0
