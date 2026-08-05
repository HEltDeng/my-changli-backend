$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Get-Command python -ErrorAction Stop

Push-Location $ProjectRoot
try {
    & $Python.Source .\manage.py has-admin
    if ($LASTEXITCODE -ne 0) {
        Write-Host "First use: create a local administrator account." -ForegroundColor Cyan
        $AdminName = Read-Host "Administrator username"
        & $Python.Source .\manage.py create-admin $AdminName
        if ($LASTEXITCODE -ne 0) {
            throw "Administrator was not created. The server was not started."
        }
    }

    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8787/health" -TimeoutSec 2
    } catch {
        $Health = $null
    }
    if (-not $Health) {
        Start-Process -FilePath $Python.Source `
            -ArgumentList @("server.py") `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden
        $Ready = $false
        for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
            Start-Sleep -Milliseconds 200
            try {
                $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8787/health" -TimeoutSec 1
                $Ready = $true
                break
            } catch {}
        }
        if (-not $Ready) {
            throw "Server did not start. Run python server.py here to view the error."
        }
    }

    # Local Android Debug builds use 127.0.0.1:8787. Re-create the USB reverse
    # mapping whenever a device reconnects so registration/login do not fail
    # merely because ADB dropped its transient forwarding state.
    $AdbCandidates = @(
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe",
        "adb.exe"
    )
    $Adb = $AdbCandidates | Where-Object {
        if ([System.IO.Path]::IsPathRooted($_)) { Test-Path -LiteralPath $_ }
        else { [bool](Get-Command $_ -ErrorAction SilentlyContinue) }
    } | Select-Object -First 1
    if ($Adb) {
        $Connected = @(& $Adb devices 2>$null | Select-String "\sdevice$")
        if ($Connected.Count -eq 1) {
            $DeviceSerial = (($Connected[0].Line -split "\s+")[0]).Trim()
            & $Adb -s $DeviceSerial reverse tcp:8787 tcp:8787 | Out-Null
            $Mapping = @(& $Adb -s $DeviceSerial reverse --list 2>$null) |
                Where-Object { $_ -match "tcp:8787\s+tcp:8787" }
            if ($LASTEXITCODE -eq 0 -and $Mapping) {
                Write-Host "Android local API mapping is ready (tcp:8787)." -ForegroundColor Green
            } else {
                Write-Warning "Backend is ready, but Android USB port mapping could not be verified. Reconnect USB debugging and run this script again."
            }
        } elseif ($Connected.Count -gt 1) {
            Write-Warning "Multiple USB debug devices are connected. Select one with 'adb -s <serial> reverse tcp:8787 tcp:8787'."
        } else {
            Write-Host "No USB debug device detected; browser admin remains available." -ForegroundColor DarkYellow
        }
    }

    Start-Process "http://127.0.0.1:8787/admin/"
    Write-Host "My Changli admin is ready: http://127.0.0.1:8787/admin/" -ForegroundColor Green
} finally {
    Pop-Location
}
