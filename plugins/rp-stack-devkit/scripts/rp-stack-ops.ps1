param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("local_revision", "server_revision", "ansible_status", "compose_status", "http_smoke", "gateway_test", "recent_logs", "provider_summary", "request_trace", "loop_probe", "causal_probe", "service_llm_trace", "backup_status")]
    [string]$Action,
    [ValidateSet("smoke", "training", "full")]
    [string]$Scope = "smoke",
    [ValidateSet("rp-gateway", "rp-light-gui", "rp-showcase-gui")]
    [string]$Service = "rp-gateway",
    [ValidateRange(1, 500)]
    [int]$Lines = 100,
    [string]$RequestId = "",
    [string]$PartyId = "",
    [string]$Expectation = "",
    [int]$Turn = 0
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "RpStackOps.psm1") -Force

$arguments = @{
    scope = $Scope
    service = $Service
    lines = $Lines
}
if (-not [string]::IsNullOrWhiteSpace($RequestId)) {
    $arguments.request_id = $RequestId
}
if (-not [string]::IsNullOrWhiteSpace($PartyId)) {
    $arguments.party_id = $PartyId
}
if (-not [string]::IsNullOrWhiteSpace($Expectation)) {
    $arguments.expectation = $Expectation
}
if ($Turn -gt 0) {
    $arguments.turn = $Turn
}

try {
    Invoke-RpStackOperation -Action $Action -Arguments $arguments | ConvertTo-Json -Depth 12
} catch {
    [ordered]@{
        action = $Action
        ok = $false
        error = $_.Exception.Message
    } | ConvertTo-Json -Depth 6
    exit 1
}
