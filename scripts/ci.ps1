param(
    [switch]$SkipGatewayTests
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$rpRoot = Join-Path $repoRoot "roles\apps\files\rp-stack"
$gatewayRoot = Join-Path $rpRoot "rp-gateway"
$gatewayTestDeps = Join-Path $gatewayRoot ".test-deps"

$rpVerificationRelativePaths = @(
    "tests\test_rp_gateway_integration.py"
    "tests\test_rp_gateway_lifecycle.py"
    "tests\test_rp_mechanics.py"
    "tests\test_rp_narrator_memory.py"
    "tests\test_rp_provider.py"
    "tests\test_rp_runner.py"
    "tests\test_rp_turn_engine.py"
    "tests\test_rp_world_scenario.py"
)
$rpVerificationFiles = @(
    $rpVerificationRelativePaths | ForEach-Object {
        Get-Item -LiteralPath (Join-Path $gatewayRoot $_)
    }
)
[int]$rpVerificationLoc = ($rpVerificationFiles |
    ForEach-Object { (Get-Content -LiteralPath $_.FullName).Count } |
    Measure-Object -Sum).Sum
$rpVerificationDebt = [Math]::Max(0, $rpVerificationLoc - 5000)
Write-Host "[budget] RP production allowlist: $rpVerificationLoc / 5000 physical LOC; debt $rpVerificationDebt."
Write-Host "[budget] Scope: exactly $($rpVerificationFiles.Count) retained clean RP test files; anchors and non-executable evidence are excluded."
Write-Host "[budget] Gateway full-suite target: <=60s on each measured environment; local and GitHub results are reported separately."

function Resolve-Tool {
    param([string]$Name, [string]$OverrideEnvironmentVariable, [string]$BundledRelativePath)

    $override = [Environment]::GetEnvironmentVariable($OverrideEnvironmentVariable)
    if (-not [string]::IsNullOrWhiteSpace($override) -and (Test-Path -LiteralPath $override)) {
        return $override
    }
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        try {
            & $command.Source --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return $command.Source
            }
        } catch {
        }
    }
    $bundled = Join-Path $env:USERPROFILE $BundledRelativePath
    if (Test-Path -LiteralPath $bundled) {
        return $bundled
    }
    throw "Cannot find $Name. Set $OverrideEnvironmentVariable to its executable path."
}

function Invoke-Checked {
    param([string]$Label, [scriptblock]$Command)
    Write-Host "[ci] $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

$python = Resolve-Tool -Name "python" -OverrideEnvironmentVariable "CODEX_PYTHON" -BundledRelativePath ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$node = Resolve-Tool -Name "node" -OverrideEnvironmentVariable "CODEX_NODE" -BundledRelativePath ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"

Push-Location $repoRoot
try {
    Invoke-Checked "repository contracts" { & $python scripts/validate-repository.py }
    if ([string]::IsNullOrWhiteSpace($env:CI)) {
        Invoke-Checked "installed Codex skill drift" {
            powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/sync-codex-skills.ps1 -Mode Check
        }
    }
    Invoke-Checked "devkit policy and MCP" { powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/test-devkit.ps1 }
    Invoke-Checked "Gateway syntax" { & $python -m compileall -q roles/apps/files/rp-stack/rp-gateway/app }

    $javascriptFiles = @(
        (Join-Path $rpRoot "rp-light-gui\app.js")
    )
    foreach ($file in $javascriptFiles) {
        Invoke-Checked "JavaScript syntax: $($file.Substring($repoRoot.Length + 1))" { & $node --check $file }
    }
    $javascriptTests = @(
        Get-ChildItem -LiteralPath (Join-Path $rpRoot "rp-light-gui") -File -Filter "*.test.js" |
            Sort-Object FullName
    )
    foreach ($test in $javascriptTests) {
        Invoke-Checked "JavaScript test: $($test.FullName.Substring($repoRoot.Length + 1))" { & $node $test.FullName }
    }

    if (-not $SkipGatewayTests) {
        & $python -c "import pytest" *> $null
        $pytestAvailable = $LASTEXITCODE -eq 0
        if (-not $pytestAvailable -and -not (Test-Path -LiteralPath (Join-Path $gatewayTestDeps "pytest"))) {
            Write-Host "[ci] restoring declared Gateway dependencies into $gatewayTestDeps"
            Invoke-Checked "Gateway dependency restore" {
                & $python -m pip install --disable-pip-version-check --target $gatewayTestDeps -r (Join-Path $gatewayRoot "requirements.txt")
            }
        }
        $previousPythonPath = $env:PYTHONPATH
        if (Test-Path -LiteralPath $gatewayTestDeps) {
            $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
                $gatewayTestDeps
            } else {
                "$gatewayTestDeps$([IO.Path]::PathSeparator)$previousPythonPath"
            }
        }
        Push-Location $gatewayRoot
        $gatewayPytestTimer = [Diagnostics.Stopwatch]::StartNew()
        try {
            Invoke-Checked "Gateway full pytest suite" { & $python -m pytest -q }
        } finally {
            $gatewayPytestTimer.Stop()
            Write-Host "[budget] Local Gateway full suite: $([Math]::Round($gatewayPytestTimer.Elapsed.TotalSeconds, 1))s / 60s."
            Write-Host "[budget] GitHub Gateway full suite: measured by the separate PR job; no local-to-runner time substitution."
            Pop-Location
            $env:PYTHONPATH = $previousPythonPath
        }
    } else {
        Write-Host "[budget] Local Gateway full suite not measured (-SkipGatewayTests)."
        Write-Host "[budget] GitHub Gateway full suite remains a separate PR gate."
    }
} finally {
    Pop-Location
}

Write-Host "[ci] all requested local gates passed"
