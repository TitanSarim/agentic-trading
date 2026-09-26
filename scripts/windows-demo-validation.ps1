# Demo validation checklist for Windows LAN (IC Markets demo + Ollama .22)
#
# Prerequisites:
#   1. Clone/pull https://github.com/TitanSarim/agentic-trading
#   2. Python 3.11+ venv with:  pip install -e ".[dev]"
#   3. pip install MetaTrader5
#   4. Start: C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe
#      (logged into DEMO account — never live for this script)
#   5. Confirm LAN: curl http://192.168.8.22:11434/api/tags
#
# Usage (from repo root):
#   .\scripts\windows-demo-validation.ps1
#   .\scripts\windows-demo-validation.ps1 -SkipSubmit   # dry-run only (default)
#   .\scripts\windows-demo-validation.ps1 -SubmitTiny   # one tiny demo fill after dry-run OK
#
# Paste the generated reports\windows-demo-validation-*.txt into
# docs/demo-validation-results.md (LAN / model / MT5 rows).

param(
    [switch]$SubmitTiny,
    [string]$OllamaUrl = "http://192.168.8.22:11434",
    [string]$PrimaryModel = "qwen3.8:27b",
    [string]$OutDir = "reports"
)

$ErrorActionPreference = "Continue"
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$log = Join-Path $OutDir "windows-demo-validation-$ts.txt"

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "o"), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line
}

function Invoke-Step([string]$name, [scriptblock]$block) {
    Write-Log "=== BEGIN $name ==="
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        & $block 2>&1 | ForEach-Object {
            $s = "$_"
            Write-Host $s
            Add-Content -Path $log -Value $s
        }
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } catch {
        Write-Log "EXCEPTION: $_"
        $code = 1
    }
    $sw.Stop()
    Write-Log ("=== END {0} exit={1} elapsed_sec={2:N3} ===" -f $name, $code, $sw.Elapsed.TotalSeconds)
    return $code
}

Write-Log "Windows demo validation log -> $log"
Write-Log "Host=$env:COMPUTERNAME User=$env:USERNAME OllamaUrl=$OllamaUrl PrimaryModel=$PrimaryModel"
Write-Log "NO LIVE / REAL-MONEY TRADING — demo account only"

$env:OLLAMA_BASE_URL = $OllamaUrl
$env:TRADER_BROKER_BACKEND = "mt5"
$env:TRADER_BROKER_MODE = "demo"

# Prefer uv if present, else trader on PATH / python -m
function Invoke-Trader([string[]]$TraderArgs) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv run trader @TraderArgs
        return $LASTEXITCODE
    }
    if (Get-Command trader -ErrorAction SilentlyContinue) {
        & trader @TraderArgs
        return $LASTEXITCODE
    }
    & python -m apps.trader.cli @TraderArgs
    return $LASTEXITCODE
}

$results = @{}

$results["curl_tags"] = Invoke-Step "curl Ollama /api/tags" {
    curl.exe -sS --connect-timeout 3 --max-time 10 "$OllamaUrl/api/tags"
}

$results["pytest"] = Invoke-Step "pytest" {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv run pytest
    } else {
        python -m pytest -q
    }
}

$results["health"] = Invoke-Step "trader health" { Invoke-Trader @("health") }
$results["config"] = Invoke-Step "trader config-print" { Invoke-Trader @("config-print") }
$results["status"] = Invoke-Step "trader status" { Invoke-Trader @("status"; "--json") }
$results["ollama_tags"] = Invoke-Step "trader ollama-tags" { Invoke-Trader @("ollama-tags") }

$results["analyze_live"] = Invoke-Step "trader analyze --live (primary model)" {
    Invoke-Trader @(
        "analyze"; "--live"; "--symbol"; "EURUSD"; "--timeframe"; "M5"; "--json"
    )
}

# Spot-check alternate models via Ollama generate if tags OK
$results["spot_qwen36"] = Invoke-Step "spot-check qwen3.6:27b" {
    $body = @{
        model = "qwen3.6:27b"
        prompt = "Reply with exactly one word: PONG"
        stream = $false
        options = @{ num_predict = 8 }
    } | ConvertTo-Json -Depth 5
    curl.exe -sS --connect-timeout 3 --max-time 120 `
        -H "Content-Type: application/json" `
        -d $body `
        "$OllamaUrl/api/generate"
}

$results["spot_qwen35"] = Invoke-Step "spot-check qwen3.5:9b" {
    $body = @{
        model = "qwen3.5:9b"
        prompt = "Reply with exactly one word: PONG"
        stream = $false
        options = @{ num_predict = 8 }
    } | ConvertTo-Json -Depth 5
    curl.exe -sS --connect-timeout 3 --max-time 60 `
        -H "Content-Type: application/json" `
        -d $body `
        "$OllamaUrl/api/generate"
}

$results["scan_live"] = Invoke-Step "trader scan --analyze --live-llm" {
    Invoke-Trader @(
        "scan"; "--symbol"; "EURUSD"; "--timeframe"; "M5"; "--bars"; "120";
        "--analyze"; "--live-llm"; "--json"
    )
}

$results["validate"] = Invoke-Step "trader validate" {
    Invoke-Trader @("validate"; "--symbol"; "EURUSD"; "--timeframe"; "M5"; "--bars"; "400"; "--ticks"; "40"; "--json")
}
$results["walk_forward"] = Invoke-Step "trader walk-forward" {
    Invoke-Trader @("walk-forward"; "--symbol"; "EURUSD"; "--bars"; "400"; "--json")
}
$results["stress"] = Invoke-Step "trader stress" {
    Invoke-Trader @("stress"; "--symbol"; "EURUSD"; "--bars"; "400"; "--json")
}
$results["soak"] = Invoke-Step "trader soak" {
    Invoke-Trader @("soak"; "--ticks"; "40"; "--db"; "data/soak.db"; "--json")
}

$results["execute_dry"] = Invoke-Step "trader execute-demo mt5 dry-run" {
    Invoke-Trader @(
        "execute-demo"; "--backend"; "mt5"; "--dry-run"; "--mock-llm"; "--json"
    )
}

if ($SubmitTiny) {
    Write-Log "OPTIONAL: tiny demo submit (confirm IC Markets DEMO account is active)"
    $results["execute_submit"] = Invoke-Step "trader execute-demo mt5 --submit --confirm-demo" {
        Invoke-Trader @(
            "execute-demo"; "--backend"; "mt5"; "--submit"; "--confirm-demo";
            "--mock-llm"; "--json"
        )
    }
} else {
    Write-Log "SKIP submit — dry-run only (pass -SubmitTiny to place one demo fill)"
    $results["execute_submit"] = "SKIPPED"
}

$results["validate_demo"] = Invoke-Step "trader validate-demo" {
    Invoke-Trader @(
        "validate-demo"; "--no-pytest"; "--symbol"; "EURUSD";
        "--timeframe"; "M5"; "--bars"; "400"; "--ticks"; "40"; "--json"
    )
}

Write-Log "======== SUMMARY ========"
foreach ($k in $results.Keys | Sort-Object) {
    Write-Log ("{0} = {1}" -f $k, $results[$k])
}
Write-Log "Log file: $((Resolve-Path $log).Path)"
Write-Log "Paste relevant sections into demo-validation-results.md LAN / model / MT5 tables."
Write-Host ""
Write-Host "Done. Log: $log" -ForegroundColor Green
