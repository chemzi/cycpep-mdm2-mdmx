$ErrorActionPreference = "Stop"
$uiRoot = $PSScriptRoot
$repoRoot = Split-Path -Parent $uiRoot
$adapter = Join-Path $repoRoot "web_api\server.py"
$apiProcess = Start-Process python -ArgumentList @($adapter, "--host", "127.0.0.1", "--port", "8765") -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru

try {
    Write-Host "CycPep data adapter: http://127.0.0.1:8765/api/v1"
    Write-Host "In the UI Connection page, use that address and choose same-host mode."
    Set-Location $uiRoot
    npm run dev
}
finally {
    if ($apiProcess -and -not $apiProcess.HasExited) {
        Stop-Process -Id $apiProcess.Id
    }
}
