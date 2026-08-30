param(
    [switch]$SkipGatewayTests,
    [switch]$IncludeSemanticAcceptance
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$rpRoot = Join-Path $repoRoot "roles\apps\files\rp-stack"
$gatewayRoot = Join-Path $rpRoot "rp-gateway"
$gatewayTestDeps = Join-Path $gatewayRoot ".test-deps"

$rpVerificationFiles = @(
    Get-ChildItem (Join-Path $gatewayRoot "tests") -Recurse -File -Filter "*.py"
    Get-ChildItem (Join-Path $rpRoot "evals") -Recurse -File -Filter "*.py"
    Get-ChildItem (Join-Path $rpRoot "scripts") -File -Filter "*.py" |
        Where-Object { $_.Name -like "test-*.py" -or $_.Name -eq "validate-relationships.py" }
)
[int]$rpVerificationLoc = ($rpVerificationFiles |
    ForEach-Object { (Get-Content -LiteralPath $_.FullName).Count } |
    Measure-Object -Sum).Sum
$rpVerificationDebt = [Math]::Max(0, $rpVerificationLoc - 5000)
Write-Host "[budget] RP verification: $rpVerificationLoc / 5000 LOC at cutover; debt $rpVerificationDebt; full <=60s GitHub, focused <=30s local."
Write-Host "[budget] Scope: $($rpVerificationFiles.Count) retained RP verification files; all count conservatively."

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
    if ($IncludeSemanticAcceptance) {
        Invoke-Checked "semantic acceptance (saved responses, no providers)" {
            powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-rp-stack-evals.ps1 -Mode SemanticAcceptance
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:CI)) {
        Invoke-Checked "installed Codex skill drift" {
            powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/sync-codex-skills.ps1 -Mode Check
        }
    }
    Invoke-Checked "devkit policy and MCP" { powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/test-devkit.ps1 }
    Push-Location $rpRoot
    try {
        $stateSeeds = @(Get-ChildItem -Path worldpacks -Recurse -Filter "state-seed.json" | Sort-Object FullName)
        foreach ($stateSeed in $stateSeeds) {
            Invoke-Checked "state schema: $($stateSeed.FullName.Substring($rpRoot.Length + 1))" {
                & $python scripts/validate-state.py --state $stateSeed.FullName --schema state/schema.json
            }
        }
        Invoke-Checked "relationship models" { & $python scripts/validate-relationships.py --worldpacks worldpacks }
        Invoke-Checked "state workflow" { & $python scripts/test-state-workflow.py }
        Invoke-Checked "check workflow" { & $python scripts/test-check-workflow.py }
    } finally {
        Pop-Location
    }
    Invoke-Checked "Gateway syntax" { & $python -m compileall -q roles/apps/files/rp-stack/rp-gateway/app }

    $javascriptFiles = @(
        (Join-Path $rpRoot "rp-light-gui\app.js")
    )
    foreach ($file in $javascriptFiles) {
        Invoke-Checked "JavaScript syntax: $($file.Substring($repoRoot.Length + 1))" { & $node --check $file }
    }
    $javascriptTests = @(
        & git -C $repoRoot ls-files "roles/apps/files/rp-stack/**/*.test.js" |
            ForEach-Object { Get-Item -LiteralPath (Join-Path $repoRoot $_) } |
            Sort-Object FullName
    )
    foreach ($test in $javascriptTests) {
        Invoke-Checked "JavaScript test: $($test.FullName.Substring($repoRoot.Length + 1))" { & $node $test.FullName }
    }

    if (-not $SkipGatewayTests) {
        if (-not (Test-Path -LiteralPath (Join-Path $gatewayTestDeps "pytest"))) {
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
            Invoke-Checked "Gateway pytest" { & $python -m pytest -q }
        } finally {
            $gatewayPytestTimer.Stop()
            Write-Host "[budget] Mixed local Gateway pytest: $([Math]::Round($gatewayPytestTimer.Elapsed.TotalSeconds, 1))s; cutover target <=60s on GitHub runner."
            Pop-Location
            $env:PYTHONPATH = $previousPythonPath
        }
    } else {
        Write-Host "[budget] Mixed local Gateway pytest runtime not measured (-SkipGatewayTests)."
    }
} finally {
    Pop-Location
}

Write-Host "[ci] all requested local gates passed"
