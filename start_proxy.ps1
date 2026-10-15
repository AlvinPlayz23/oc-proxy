# Launch the OpenCode compat shim, detached, idempotent.
# Picks a python that actually has aiohttp, is idempotent (no-op if the port
# already answers), and waits for /health before exiting.
param([int]$Port = 18788)

$root   = $PSScriptRoot
$src    = Join-Path $root 'opencode_proxy.py'
$log    = Join-Path $env:TEMP 'oc_proxy.log'
$err    = Join-Path $env:TEMP 'oc_proxy.err'
$slog   = Join-Path $env:TEMP 'oc_starter.log'

# The proxy resolves the CLI itself ($OPENCODE_CLI -> PATH -> desktop app), so
# there is nothing to configure here.
$cands = @(
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe')
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

# Add-Content opens/writes/closes, so two starters racing at logon cannot
# collide the way a cmd.exe ">>" redirect does (it opens exclusively).
function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    try { Add-Content -Path $slog -Value $line } catch { }
    Write-Host $line
}

# Already healthy? Then there is nothing to do.
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
    Log "already listening on $Port (default=$($h.default_model)) - no-op"
    exit 0
} catch { }

$py = $null
foreach ($c in $cands) {
    & $c -c "import aiohttp" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}
if (-not $py) {
    Log 'FATAL: no python with aiohttp found. Try: pip install aiohttp'
    exit 1
}

Log "interpreter: $py"
Start-Process -WindowStyle Hidden $py -ArgumentList $src, '--port', $Port `
    -RedirectStandardOutput $log -RedirectStandardError $err

for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        Log "up after ${i}s (default=$($h.default_model), $($h.free_models.Count) free models)"
        if (-not $h.cli_exists) {
            Log "WARNING: CLI not found at $($h.cli). Set `$env:OPENCODE_CLI to override."
        }
        exit 0
    } catch { }
}

Log 'FAILED to start'
if (Test-Path $err) { Get-Content $err -Tail 20 | ForEach-Object { Log "  $_" } }
exit 1
