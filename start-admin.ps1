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

    Start-Process "http://127.0.0.1:8787/admin/"
    Write-Host "My Changli admin is ready: http://127.0.0.1:8787/admin/" -ForegroundColor Green
} finally {
    Pop-Location
}
