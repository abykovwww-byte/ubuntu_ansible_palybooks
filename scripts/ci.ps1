param(
    [switch]$SkipGatewayTests
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$rpRoot = Join-Path $repoRoot "roles\apps\files\rp-stack"
$gatewayRoot = Join-Path $rpRoot "rp-gateway"
$gatewayTestDeps = Join-Path $gatewayRoot ".test-deps"

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
    Push-Location $rpRoot
    try {
        $stateSeeds = @(Get-ChildItem -Path worldpacks -Recurse -Filter "state-seed.json" | Sort-Object FullName)
        foreach ($stateSeed in $stateSeeds) {
            Invoke-Checked "state schema: $($stateSeed.FullName.Substring($rpRoot.Length + 1))" {
                & $python scripts/validate-state.py --state $stateSeed.FullName --schema state/schema.json
            }
        }
        Invoke-Checked "training runtime" { & $python scripts/validate-training-runtime.py --worldpacks worldpacks }
        Invoke-Checked "relationship models" { & $python scripts/validate-relationships.py --worldpacks worldpacks }
        Invoke-Checked "state workflow" { & $python scripts/test-state-workflow.py }
        Invoke-Checked "check workflow" { & $python scripts/test-check-workflow.py }
    } finally {
        Pop-Location
    }
    Invoke-Checked "Gateway syntax" { & $python -m compileall -q roles/apps/files/rp-stack/rp-gateway/app }

    $javascriptFiles = @(
        (Join-Path $rpRoot "rp-light-gui\app.js"),
        (Join-Path $rpRoot "rp-showcase-gui\app.js")
    )
    foreach ($file in $javascriptFiles) {
        Invoke-Checked "JavaScript syntax: $($file.Substring($repoRoot.Length + 1))" { & $node --check $file }
    }
    $javascriptTests = @(Get-ChildItem -Path $rpRoot -Recurse -Filter "*.test.js" | Sort-Object FullName)
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
        try {
            Invoke-Checked "Gateway pytest" { & $python -m pytest -q }
        } finally {
            Pop-Location
            $env:PYTHONPATH = $previousPythonPath
        }
    }
} finally {
    Pop-Location
}

Write-Host "[ci] all requested local gates passed"
