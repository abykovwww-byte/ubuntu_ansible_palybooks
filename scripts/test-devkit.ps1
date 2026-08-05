$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectHook = Join-Path $repoRoot ".codex\hooks\rp_stack_policy.ps1"
$pluginHook = Join-Path $repoRoot "plugins\rp-stack-devkit\hooks\rp_stack_policy.ps1"
$opsScript = Join-Path $repoRoot "plugins\rp-stack-devkit\scripts\rp-stack-ops.ps1"
$mcpScript = Join-Path $repoRoot "plugins\rp-stack-devkit\scripts\mcp-server.ps1"
$powerShellExecutable = (Get-Process -Id $PID).Path

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
    }
}

function Invoke-Hook {
    param([hashtable]$Payload)
    $json = $Payload | ConvertTo-Json -Depth 10 -Compress
    return ($json | & $powerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $projectHook | Out-String).Trim()
}

$denyReset = Invoke-Hook @{ tool_name = "shell_command"; tool_input = @{ command = "git reset --hard HEAD" } }
Assert-True ($denyReset -match '"permissionDecision":"deny"') "Hook did not deny git reset --hard."

$pluginDenyReset = ((@{ tool_name = "shell_command"; tool_input = @{ command = "git reset --hard HEAD" } } | ConvertTo-Json -Depth 10 -Compress) | & $powerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $pluginHook | Out-String).Trim()
Assert-True ($pluginDenyReset -match '"permissionDecision":"deny"') "Plugin hook did not deny git reset --hard."

$denySecret = Invoke-Hook @{ tool_name = "shell_command"; tool_input = @{ command = "Get-Content /etc/ansible/local-overrides.yml" } }
Assert-True ($denySecret -match '"permissionDecision":"deny"') "Hook did not deny server-only override access."

$denyLiteralSecret = Invoke-Hook @{ tool_name = "shell_command"; tool_input = @{ command = "curl -H 'Authorization: Bearer this-is-a-realistic-secret-value' https://example.invalid" } }
Assert-True ($denyLiteralSecret -match '"permissionDecision":"deny"') "Hook did not deny a probable literal credential."

$denyRuntimeWrite = Invoke-Hook @{ tool_name = "shell_command"; tool_input = @{ command = "cd /srv/apps/rp-stack && docker compose restart" } }
Assert-True ($denyRuntimeWrite -match '"permissionDecision":"deny"') "Hook did not deny direct runtime mutation."

$allowStatus = Invoke-Hook @{ tool_name = "shell_command"; tool_input = @{ command = "git status --short" } }
Assert-True ([string]::IsNullOrWhiteSpace($allowStatus)) "Hook unexpectedly denied read-only git status."

$allowSourceToken = Invoke-Hook @{ tool_name = "apply_patch"; tool_input = @{ patch = "token = parse_token(value)" } }
Assert-True ([string]::IsNullOrWhiteSpace($allowSourceToken)) "Hook unexpectedly denied an ordinary source-code token variable."

$fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("rp-stack-ops-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $fixtureRoot | Out-Null
try {
    Set-Content -LiteralPath (Join-Path $fixtureRoot "provider_summary.txt") -Encoding UTF8 -Value "Authorization: Bearer secret-value`napi_key=sk-this-must-be-redacted`nfallback_reason=validation_failed"
    $env:RP_STACK_OPS_FIXTURE_DIR = $fixtureRoot
    $result = & $opsScript -Action provider_summary -Lines 20 | ConvertFrom-Json
    Assert-True $result.ok "Fixture-backed provider summary failed."
    Assert-True ($result.output -notmatch 'secret-value|sk-this-must-be-redacted') "Ops output leaked a probable credential."
    Assert-True ($result.output -match 'fallback_reason=validation_failed') "Ops output lost non-secret diagnostic evidence."
} finally {
    Remove-Item Env:RP_STACK_OPS_FIXTURE_DIR -ErrorAction SilentlyContinue
    if ((Resolve-Path $fixtureRoot).Path.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
    }
}

$mcpRequests = @(
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}',
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"request_trace","arguments":{"request_id":"bad id"}}}'
)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$mcpResponses = @($mcpRequests | & $powerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $mcpScript)
Assert-True ($mcpResponses.Count -eq 3) "MCP server did not return three responses."
$initialize = $mcpResponses[0] | ConvertFrom-Json
$toolList = $mcpResponses[1] | ConvertFrom-Json
$invalidCall = $mcpResponses[2] | ConvertFrom-Json
Assert-True ($initialize.result.serverInfo.name -eq "rp-stack-ops") "MCP initialize response is invalid."
Assert-True (($toolList.result.tools.name -contains "gateway_test") -and -not ($toolList.result.tools.name -contains "deploy")) "MCP tool allowlist is invalid."
Assert-True $invalidCall.result.isError "MCP failed to reject an invalid request ID."

Write-Host "RP Stack devkit policy, redaction, argument validation, and MCP protocol tests passed."
