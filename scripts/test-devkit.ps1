$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectHook = Join-Path $repoRoot ".codex\hooks\rp_stack_policy.ps1"
$pluginHook = Join-Path $repoRoot "plugins\rp-stack-devkit\hooks\rp_stack_policy.ps1"
$opsScript = Join-Path $repoRoot "plugins\rp-stack-devkit\scripts\rp-stack-ops.ps1"
$opsModule = Join-Path $repoRoot "plugins\rp-stack-devkit\scripts\RpStackOps.psm1"
$mcpScript = Join-Path $repoRoot "plugins\rp-stack-devkit\scripts\mcp-server.ps1"
$adr022FixtureRoot = Join-Path $repoRoot "plugins\rp-stack-devkit\tests\fixtures\adr-022"
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

Import-Module $opsModule -Force
$loadedOpsModule = Get-Module RpStackOps
$probeScripts = & $loadedOpsModule {
    @("loop_probe", "causal_probe", "service_llm_trace") | ForEach-Object {
        [ordered]@{ action = $_; script = Get-RpProbePython -Action $_ }
    }
}
foreach ($probeScript in $probeScripts) {
    Assert-True ($probeScript.script -match 'file:/data/rp_gateway\.db\?mode=ro') "$($probeScript.action) does not open SQLite in URI mode=ro."
    Assert-True ($probeScript.script -notmatch '(?im)^\s*["'']?\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|VACUUM|ATTACH|DETACH|REINDEX)\b') "$($probeScript.action) contains write-capable SQL."
}
$causalProbeSource = ($probeScripts | Where-Object { $_.action -eq "causal_probe" }).script
Assert-True ($causalProbeSource -match "seed_cause_characters") "causal_probe does not require a seed cause for event projection."
Assert-True ($causalProbeSource -match 'text\.startswith\("RELATIONSHIP_PRESSURE"\) and character_name in text') "causal_probe does not bind character evidence to one pressure block."
$registeredCausalExpectations = @(
    "seed_trust_influences_plot",
    "relationship_pressure_reaches_next_turn_prompt",
    "relationship_event_has_canonical_character_attribution",
    "relationship_badge_has_canonical_character_attribution",
    "trust_gained_reaches_next_turn_prompt"
)
foreach ($expectation in $registeredCausalExpectations) {
    Assert-True ($causalProbeSource -match [regex]::Escape(('"' + $expectation + '"'))) "causal_probe does not implement $expectation."
}
$causalTool = Get-RpStackToolDefinitions | Where-Object { $_.name -eq "causal_probe" }
$toolExpectations = @($causalTool.inputSchema.properties.expectation.enum | Sort-Object)
$expectedToolExpectations = @($registeredCausalExpectations | Sort-Object)
Assert-True (($toolExpectations -join "|") -eq ($expectedToolExpectations -join "|")) "causal_probe MCP enum differs from implemented expectations."
$rootOpsWrapper = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "scripts\rp-stack-ops.ps1")
foreach ($parameter in @("PartyId", "Expectation", "Turn")) {
    Assert-True ($rootOpsWrapper -match (('-' + $parameter + '\s+\$' + $parameter))) "Root rp-stack-ops wrapper does not forward $parameter."
}
$nativeStderrResult = & $loadedOpsModule {
    param([string]$Executable)
    Invoke-RpExternalCommand -FilePath $Executable -ArgumentList @(
        "-NoProfile",
        "-Command",
        "[Console]::Error.WriteLine('docker-progress'); exit 0"
    )
} $powerShellExecutable
Assert-True ($nativeStderrResult.exit_code -eq 0) "Native stderr incorrectly changed a successful exit code."
Assert-True ($nativeStderrResult.output -match 'docker-progress') "Native stderr was not captured in command output."

$projectHookHash = (Get-FileHash -LiteralPath $projectHook -Algorithm SHA256).Hash
$pluginHookHash = (Get-FileHash -LiteralPath $pluginHook -Algorithm SHA256).Hash
Assert-True ($projectHookHash -eq $pluginHookHash) "Project and plugin policy hooks must be byte-identical."

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
    Set-Content -LiteralPath (Join-Path $fixtureRoot "provider_summary.txt") -Encoding UTF8 -Value "Authorization: Bearer secret-value`napi_key=sk-this-must-be-redacted`n'password_hash': '`$apr1`$must-not-leak'`nfallback_reason=validation_failed"
    Set-Content -LiteralPath (Join-Path $fixtureRoot "server_revision.txt") -Encoding UTF8 -Value "fixture-server-revision"
    $env:RP_STACK_OPS_FIXTURE_DIR = $fixtureRoot
    $result = & $opsScript -Action provider_summary -Lines 20 | ConvertFrom-Json
    Assert-True $result.ok "Fixture-backed provider summary failed."
    Assert-True ($result.output -notmatch 'secret-value|sk-this-must-be-redacted|must-not-leak') "Ops output leaked a probable credential."
    Assert-True ($result.output -match 'fallback_reason=validation_failed') "Ops output lost non-secret diagnostic evidence."

    $mcpRequests = @(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}',
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"server_revision","arguments":{}}}',
        '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"request_trace","arguments":{"request_id":"bad id"}}}'
    )
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $mcpResponses = @($mcpRequests | & $powerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $mcpScript)
    Assert-True ($mcpResponses.Count -eq 4) "MCP server did not return four responses."
    $initialize = $mcpResponses[0] | ConvertFrom-Json
    $toolList = $mcpResponses[1] | ConvertFrom-Json
    $emptyArgumentsCall = $mcpResponses[2] | ConvertFrom-Json
    $invalidCall = $mcpResponses[3] | ConvertFrom-Json
    Assert-True ($initialize.result.serverInfo.name -eq "rp-stack-ops") "MCP initialize response is invalid."
    Assert-True (($toolList.result.tools.name -contains "gateway_test") -and -not ($toolList.result.tools.name -contains "deploy")) "MCP tool allowlist is invalid."
    Assert-True (($toolList.result.tools.name -contains "loop_probe") -and ($toolList.result.tools.name -contains "causal_probe") -and ($toolList.result.tools.name -contains "service_llm_trace")) "ADR 022 MCP tools are missing."
    Assert-True (-not $emptyArgumentsCall.result.isError) "MCP failed a valid call with empty arguments."
    Assert-True ($emptyArgumentsCall.result.structuredContent.source -eq "fixture") "MCP empty-arguments call did not use the fixture."
    Assert-True $emptyArgumentsCall.result.structuredContent.ok "Fixture-backed empty-argument MCP call failed."
    Assert-True $invalidCall.result.isError "MCP failed to reject an invalid request ID."
} finally {
    Remove-Item Env:RP_STACK_OPS_FIXTURE_DIR -ErrorAction SilentlyContinue
    if ((Resolve-Path $fixtureRoot).Path.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
    }
}

$env:RP_STACK_OPS_FIXTURE_DIR = Join-Path $adr022FixtureRoot "complete"
try {
    $loop = Invoke-RpStackOperation -Action loop_probe -Arguments @{ party_id = "party_fixture_complete" }
    Assert-True $loop.ok "Complete loop_probe fixture failed."
    Assert-True $loop.necessary_not_sufficient "loop_probe did not mark counters necessary-not-sufficient."
    Assert-True ($loop.operations_total -eq 4 -and $loop.operations_outside_timeline -eq 2) "loop_probe counters changed."
    Assert-True ($loop.operation_counters.add -eq 3 -and $loop.nonempty_extraction_share.share -eq 0.5) "loop_probe detail counters changed."

    $causal = Invoke-RpStackOperation -Action causal_probe -Arguments @{
        party_id = "party_fixture_complete"
        expectation = "seed_trust_influences_plot"
    }
    Assert-True ($causal.ok -and $causal.passed) "Complete causal chain did not pass."
    Assert-True ($null -eq $causal.break_at) "Complete causal chain reported a break."
    Assert-True (@($causal.steps | Where-Object { -not $_.passed }).Count -eq 0) "Complete causal chain contains a failed step."

    $trace = Invoke-RpStackOperation -Action service_llm_trace -Arguments @{
        party_id = "party_fixture_complete"
        turn = 5
    }
    Assert-True ($trace.ok -and @($trace.records).Count -eq 1) "service_llm_trace fixture failed."
    Assert-True ($trace.records[0].prompt_text -eq "Extract events exactly. Authorization=[REDACTED]") "service_llm_trace did not preserve and redact the exact prompt."
    Assert-True ($trace.records[0].raw_response -match '\[REDACTED(?:_API_KEY)?\]') "service_llm_trace did not mark API-key redaction."
    Assert-True ($trace.records[0].raw_response -notmatch 'fixturesecret') "service_llm_trace leaked fixture secret material."

    $mcpProbeRequests = @(
        '{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"loop_probe","arguments":{"party_id":"party_fixture_complete"}}}',
        '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"causal_probe","arguments":{"party_id":"party_fixture_complete","expectation":"seed_trust_influences_plot"}}}',
        '{"jsonrpc":"2.0","id":13,"method":"tools/call","params":{"name":"service_llm_trace","arguments":{"party_id":"party_fixture_complete","turn":5}}}'
    )
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $mcpProbeResponses = @($mcpProbeRequests | & $powerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $mcpScript)
    Assert-True ($mcpProbeResponses.Count -eq 3) "MCP server did not return all ADR 022 probe responses."
    foreach ($responseText in $mcpProbeResponses) {
        $response = $responseText | ConvertFrom-Json
        Assert-True (-not $response.result.isError) "MCP returned an error for a valid ADR 022 fixture."
        Assert-True ($response.result.structuredContent.source -eq "fixture") "ADR 022 MCP call did not use its deterministic fixture."
    }
} finally {
    Remove-Item Env:RP_STACK_OPS_FIXTURE_DIR -ErrorAction SilentlyContinue
}

$env:RP_STACK_OPS_FIXTURE_DIR = Join-Path $adr022FixtureRoot "broken-prompt-presence"
try {
    $broken = Invoke-RpStackOperation -Action causal_probe -Arguments @{
        party_id = "party_fixture_broken"
        expectation = "seed_trust_influences_plot"
    }
    Assert-True ($broken.ok -and -not $broken.passed) "Broken causal fixture unexpectedly passed."
    Assert-True ($broken.break_at -eq "prompt_presence") "Broken causal fixture did not identify prompt_presence as the break."
    Assert-True (($broken.steps | Where-Object { $_.step -eq "prompt_presence" }).passed -eq $false) "Broken causal fixture did not mark prompt_presence failed."
} finally {
    Remove-Item Env:RP_STACK_OPS_FIXTURE_DIR -ErrorAction SilentlyContinue
}

$env:RP_STACK_OPS_FIXTURE_DIR = Join-Path $adr022FixtureRoot "missing-service-log"
try {
    $missingTrace = Invoke-RpStackOperation -Action service_llm_trace -Arguments @{
        party_id = "party_fixture_complete"
        turn = 5
    }
    Assert-True (-not $missingTrace.ok -and $missingTrace.error_code -eq "service_call_log_missing") "Missing service_call_log was not reported as a structured probe failure."
} finally {
    Remove-Item Env:RP_STACK_OPS_FIXTURE_DIR -ErrorAction SilentlyContinue
}

$invalidCalls = @(
    { Invoke-RpStackOperation -Action loop_probe -Arguments @{ party_id = "bad id" } },
    { Invoke-RpStackOperation -Action loop_probe -Arguments @{ party_id = 123 } },
    { Invoke-RpStackOperation -Action causal_probe -Arguments @{ party_id = "party_valid"; expectation = "unregistered" } },
    { Invoke-RpStackOperation -Action service_llm_trace -Arguments @{ party_id = "party_valid"; turn = "1" } },
    { Invoke-RpStackOperation -Action service_llm_trace -Arguments @{ party_id = "party_valid"; turn = 0 } }
)
foreach ($invalidCall in $invalidCalls) {
    $rejected = $false
    try {
        & $invalidCall | Out-Null
    } catch {
        $rejected = $true
    }
    Assert-True $rejected "ADR 022 probe accepted invalid input."
}

Write-Host "RP Stack devkit policy, redaction, argument validation, and MCP protocol tests passed."
