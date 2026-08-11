param(
    [Parameter(Mandatory = $true)][string]$Action,
    [string]$Scope = "smoke",
    [string]$Service = "rp-gateway",
    [int]$Lines = 100,
    [string]$RequestId = "",
    [string]$PartyId = "",
    [string]$Expectation = "",
    [int]$Turn = 0
)

$entrypoint = Join-Path $PSScriptRoot "..\plugins\rp-stack-devkit\scripts\rp-stack-ops.ps1"
& $entrypoint -Action $Action -Scope $Scope -Service $Service -Lines $Lines -RequestId $RequestId `
    -PartyId $PartyId -Expectation $Expectation -Turn $Turn
exit $LASTEXITCODE
