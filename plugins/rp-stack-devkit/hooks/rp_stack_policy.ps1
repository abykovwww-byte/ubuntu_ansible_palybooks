$ErrorActionPreference = "Stop"
# Keep this file byte-identical to the plugin policy copy.

$rawInput = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($rawInput)) {
    exit 0
}

try {
    $payload = $rawInput | ConvertFrom-Json
} catch {
    exit 0
}

$toolName = ""
if ($payload.PSObject.Properties.Name -contains "tool_name") {
    $toolName = [string]$payload.tool_name
} elseif ($payload.PSObject.Properties.Name -contains "toolName") {
    $toolName = [string]$payload.toolName
}

$toolInput = $null
if ($payload.PSObject.Properties.Name -contains "tool_input") {
    $toolInput = $payload.tool_input
} elseif ($payload.PSObject.Properties.Name -contains "toolInput") {
    $toolInput = $payload.toolInput
}

$inspectionText = "{0}`n{1}" -f $toolName, ($toolInput | ConvertTo-Json -Depth 20 -Compress)
$denyReason = $null
$rules = @(
    @{ Pattern = '(?i)git\s+reset\s+--hard'; Reason = 'Hard reset is disabled in this repository; preserve user work and use a scoped revert or fix.' },
    @{ Pattern = '(?i)git\s+clean\s+-(?:[^\s]*f[^\s]*|[^\s]*x[^\s]*)'; Reason = 'Destructive git clean is disabled; inspect exact untracked targets first.' },
    @{ Pattern = '(?i)git\s+push\b[^\r\n]*(?:--force(?:-with-lease)?|\s-f(?:\s|$))'; Reason = 'Force-pushing is disabled for the RP Stack workflow.' },
    @{ Pattern = '(?i)(?:rm\s+-rf|remove-item\b[^\r\n]*-recurse)'; Reason = 'Recursive deletion requires an explicit, separately reviewed operation.' },
    @{ Pattern = '(?i)/etc/ansible/local-overrides\.yml|\\etc\\ansible\\local-overrides\.yml'; Reason = 'Server-only overrides may not be read, copied, or modified by Codex tooling.' },
    @{ Pattern = '(?i)Bearer\s+[A-Za-z0-9._~+/-]{16,}|\bsk-[A-Za-z0-9_-]{16,}\b|(?:authorization|cookie|password|api[_-]?key|secret|token)\s*[:=]\s*[\"'']?(?:sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9._~+/-]{20,})'; Reason = 'A probable plaintext credential was found in tool input.' }
)

foreach ($rule in $rules) {
    if ($inspectionText -match $rule.Pattern) {
        $denyReason = $rule.Reason
        break
    }
}

if (-not $denyReason) {
    $runtimePath = '(?i)/(?:srv/apps/rp-stack|srv/app-data/rp-stack|srv/backups/rp-stack)'
    $runtimeMutation = '(?i)(?:\bsed\s+-i\b|\btee\b|\bcp\b|\bmv\b|\brm\b|\bchmod\b|\bchown\b|docker\s+compose\s+(?:up|down|restart)\b)'
    if ($inspectionText -match $runtimePath -and $inspectionText -match $runtimeMutation) {
        $denyReason = 'Direct runtime mutation is disabled; change IaC, publish it, then use the pull-based Ansible service.'
    }
}

if ($denyReason) {
    [ordered]@{
        hookSpecificOutput = [ordered]@{
            hookEventName = "PreToolUse"
            permissionDecision = "deny"
            permissionDecisionReason = $denyReason
        }
    } | ConvertTo-Json -Depth 8 -Compress
}

exit 0
