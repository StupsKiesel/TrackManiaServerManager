@echo off
REM ============================================================
REM  TrackMania WSL2 connection helper for Windows 10
REM
REM  Sets up TCP portproxy + Windows firewall rules and runs an
REM  in-process PowerShell UDP relay (netsh cannot proxy UDP).
REM  Leave the window open while you play; Ctrl-C to stop.
REM
REM  Usage:
REM    wsl-tm.bat              relay default port 2350
REM    wsl-tm.bat 2360         relay a different port
REM    wsl-tm.bat 2350 clear   remove all rules for that port
REM ============================================================
setlocal EnableDelayedExpansion

REM --- self-elevate -------------------------------------------------
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

REM --- args --------------------------------------------------------
set "PORT=%~1"
if "%PORT%"=="" set "PORT=2350"
set "MODE=%~2"

REM --- get WSL IP --------------------------------------------------
for /f "usebackq tokens=1" %%I in (`wsl hostname -I`) do (
    set "WSL_IP=%%I"
    goto :got_ip
)
:got_ip
if "%WSL_IP%"=="" (
    echo [ERROR] Could not get WSL IP. Is WSL running?
    pause & exit /b 1
)

if /i "%MODE%"=="clear" goto :clear

echo ============================================================
echo  WSL IP : %WSL_IP%
echo  Port   : %PORT%  (TCP via portproxy, UDP via this window)
echo ============================================================
echo.

REM --- TCP portproxy (refresh) -------------------------------------
netsh interface portproxy delete v4tov4 listenport=%PORT% listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy add    v4tov4 listenport=%PORT% listenaddress=0.0.0.0 connectport=%PORT% connectaddress=%WSL_IP% >nul

REM --- firewall (idempotent) ---------------------------------------
netsh advfirewall firewall delete rule name="TM Dedicated TCP %PORT%" >nul 2>&1
netsh advfirewall firewall delete rule name="TM Dedicated UDP %PORT%" >nul 2>&1
netsh advfirewall firewall add rule name="TM Dedicated TCP %PORT%" dir=in action=allow protocol=TCP localport=%PORT% >nul
netsh advfirewall firewall add rule name="TM Dedicated UDP %PORT%" dir=in action=allow protocol=UDP localport=%PORT% >nul

echo [OK] TCP portproxy + firewall rules in place.
echo.
echo Starting UDP relay. Connect the game to:  127.0.0.1:%PORT%
echo Leave this window open. Press Ctrl-C to stop.
echo To remove all rules later:  wsl-tm.bat %PORT% clear
echo ------------------------------------------------------------
echo.

REM --- UDP relay (inline PowerShell) -------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port=%PORT%; $wsl='%WSL_IP%';" ^
  "$front=New-Object System.Net.Sockets.UdpClient $port;" ^
  "$back =New-Object System.Net.Sockets.UdpClient;" ^
  "$wslEp=[System.Net.IPEndPoint]::new([System.Net.IPAddress]::Parse($wsl),$port);" ^
  "$any  =[System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any,0);" ^
  "$clientEp=$null;" ^
  "Write-Host ('[relay] UDP 0.0.0.0:{0} <-> {1}:{0}' -f $port,$wsl);" ^
  "while($true){" ^
  "  if($front.Available -gt 0){" ^
  "    $ep=$any; $d=$front.Receive([ref]$ep); $script:clientEp=$ep;" ^
  "    [void]$back.Send($d,$d.Length,$wslEp);" ^
  "  }" ^
  "  if($back.Available -gt 0){" ^
  "    $ep=$any; $d=$back.Receive([ref]$ep);" ^
  "    if($script:clientEp){ [void]$front.Send($d,$d.Length,$script:clientEp) }" ^
  "  }" ^
  "  if($front.Available -eq 0 -and $back.Available -eq 0){ Start-Sleep -Milliseconds 1 }" ^
  "}"

echo.
echo Relay stopped. Cleaning up TCP portproxy...
netsh interface portproxy delete v4tov4 listenport=%PORT% listenaddress=0.0.0.0 >nul 2>&1
echo Done. (Firewall rules left in place — remove them with: wsl-tm.bat %PORT% clear)
pause
exit /b 0

:clear
netsh interface portproxy delete v4tov4 listenport=%PORT% listenaddress=0.0.0.0 >nul 2>&1
netsh advfirewall firewall delete rule name="TM Dedicated TCP %PORT%" >nul 2>&1
netsh advfirewall firewall delete rule name="TM Dedicated UDP %PORT%" >nul 2>&1
echo [OK] Removed portproxy + firewall rules for port %PORT%.
pause
exit /b 0
