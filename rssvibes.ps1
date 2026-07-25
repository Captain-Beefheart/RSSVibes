# rssvibes.ps1 — run RSSVibes as a desktop app.
#
# Starts the local server, waits for it to come up, opens a chromeless
# "app mode" browser window (Edge or Chrome), and shuts the server down
# again when that window is closed. Zero extra dependencies — it just uses
# the Python this project already runs on and a Chromium browser you have.
#
# Double-click RSSVibes.vbs (no console), or run:  powershell -File rssvibes.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = if ($env:PORT) { $env:PORT } else { '8787' }
$url  = "http://127.0.0.1:$port/"

# --- Python (prefer the MSYS2 build this project targets) ---------------
$py = 'C:\msys64\mingw64\bin\python.exe'
if (-not (Test-Path $py)) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  $py = if ($cmd) { $cmd.Source } else { 'python' }
}

# --- a Chromium browser for app mode -----------------------------------
$browser = @(
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

# --- start the server (hidden) -----------------------------------------
$env:PORT = $port
$serverPy = Join-Path $root 'server.py'
$server = Start-Process -FilePath $py -ArgumentList "`"$serverPy`"" -WindowStyle Hidden -PassThru

# --- wait for it to accept connections ---------------------------------
for ($i = 0; $i -lt 60; $i++) {
  try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', [int]$port); $c.Close(); break }
  catch { Start-Sleep -Milliseconds 200 }
}

if ($browser) {
  $profileDir = Join-Path $root 'data\app-profile'
  New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
  $bargs = @("--app=$url", "--user-data-dir=`"$profileDir`"",
             '--window-size=1280,860', '--no-first-run', '--no-default-browser-check')
  Start-Process -FilePath $browser -ArgumentList $bargs | Out-Null

  # Wait for the app window to close, then stop the server. The app-mode
  # window runs under its own profile dir, so we can watch just for it.
  $name = [IO.Path]::GetFileNameWithoutExtension($browser)
  Start-Sleep -Seconds 2
  while ($true) {
    Start-Sleep -Seconds 1
    $alive = Get-CimInstance Win32_Process -Filter "Name='$name.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*app-profile*" }
    if (-not $alive) { break }
  }
  try { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue } catch {}
}
else {
  # No Chromium browser found — open the default browser instead and leave
  # the server running (close it from Task Manager, or use start.bat).
  Start-Process $url
}
