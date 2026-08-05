param(
    [Parameter(Mandatory = $true)][string]$Action,
    [string]$Scope = "smoke",
    [string]$Service = "rp-gateway",
    [int]$Lines = 100,
    [string]$RequestId = ""
)

$entrypoint = Join-Path $PSScriptRoot "..\plugins\rp-stack-devkit\scripts\rp-stack-ops.ps1"
& $entrypoint -Action $Action -Scope $Scope -Service $Service -Lines $Lines -RequestId $RequestId
exit $LASTEXITCODE
