$ErrorActionPreference = "Stop"
$uiRoot = $PSScriptRoot
$repoRoot = Split-Path -Parent $uiRoot
$adapter = Join-Path $repoRoot "web_api\server.py"

$pythonCandidates = @()
if ($env:CYCPEP_PYTHON) { $pythonCandidates += $env:CYCPEP_PYTHON }
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) { $pythonCandidates += $pythonCommand.Source }
if ($env:CONDA_PREFIX) { $pythonCandidates += (Join-Path $env:CONDA_PREFIX "python.exe") }
$pythonCandidates += (Join-Path $env:USERPROFILE "anaconda3\python.exe")
$python = $null
foreach ($candidate in ($pythonCandidates | Select-Object -Unique)) {
    if (-not $candidate) { continue }
    try {
        & $candidate -c "import paramiko" 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    } catch { }
}
if (-not $python) {
    throw "找不到带 paramiko 的 Python。请执行 python -m pip install -r requirements.txt，或设置 CYCPEP_PYTHON 指向正确环境。"
}

$env:CYCPEP_UI_ORIGIN = "http://localhost:4173"
$apiProcess = Start-Process $python -ArgumentList @($adapter, "--host", "127.0.0.1", "--port", "8765") -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru

try {
    Write-Host "CycPep data adapter: http://127.0.0.1:8765/api/v1"
    Write-Host "CycPep Studio: http://localhost:4173"
    Set-Location $uiRoot
    npm run dev -- --host localhost --port 4173
}
finally {
    if ($apiProcess -and -not $apiProcess.HasExited) {
        Stop-Process -Id $apiProcess.Id
    }
}
