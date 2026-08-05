Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-RpStackRepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}

function Get-RpArgument {
    param(
        [AllowNull()][object]$Arguments,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][object]$Default = $null
    )

    if ($null -eq $Arguments) {
        return $Default
    }
    if ($Arguments -is [System.Collections.IDictionary] -and $Arguments.Contains($Name)) {
        return $Arguments[$Name]
    }
    if ($Arguments.PSObject.Properties.Name -contains $Name) {
        return $Arguments.$Name
    }
    return $Default
}

function Protect-RpStackOutput {
    param([AllowNull()][string]$Text)

    if ($null -eq $Text) {
        return ""
    }
    $redacted = $Text
    $redacted = [regex]::Replace($redacted, '(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*', 'Bearer [REDACTED]')
    $redacted = [regex]::Replace($redacted, '(?i)\bsk-[A-Za-z0-9_-]{8,}\b', '[REDACTED_API_KEY]')
    $redacted = [regex]::Replace(
        $redacted,
        '(?im)\b(api[_-]?key|authorization|cookie|password|secret|token)\b(\s*[:=]\s*)([^\s,;]+)',
        '$1$2[REDACTED]'
    )
    if ($redacted.Length -gt 40000) {
        $redacted = $redacted.Substring(0, 40000) + "`n[OUTPUT_TRUNCATED]"
    }
    return $redacted
}

function Invoke-RpExternalCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )

    $lines = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object { [string]$_ })
    $exitCode = $LASTEXITCODE
    return [ordered]@{
        exit_code = $exitCode
        output = ($lines -join "`n")
    }
}

function Get-RpStackSshCommand {
    if (-not [string]::IsNullOrWhiteSpace($env:RP_STACK_OPS_SSH)) {
        return $env:RP_STACK_OPS_SSH
    }
    $windowsSsh = "C:\Windows\System32\OpenSSH\ssh.exe"
    if (Test-Path -LiteralPath $windowsSsh) {
        return $windowsSsh
    }
    return "ssh"
}

function Get-RpStackHost {
    $target = $env:RP_STACK_OPS_HOST
    if ([string]::IsNullOrWhiteSpace($target)) {
        return "abykov@192.168.1.88"
    }
    if ($target -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.:-]+$') {
        throw "RP_STACK_OPS_HOST must be a simple user@host value."
    }
    return $target
}

function Invoke-RpStackRemote {
    param(
        [Parameter(Mandatory = $true)][string]$Action,
        [Parameter(Mandatory = $true)][string]$Command
    )

    if (-not [string]::IsNullOrWhiteSpace($env:RP_STACK_OPS_FIXTURE_DIR)) {
        $fixture = Join-Path $env:RP_STACK_OPS_FIXTURE_DIR ($Action + ".txt")
        if (-not (Test-Path -LiteralPath $fixture)) {
            throw "Missing RP Stack ops fixture: $fixture"
        }
        return [ordered]@{
            exit_code = 0
            output = Get-Content -Raw -LiteralPath $fixture
            source = "fixture"
        }
    }

    $sshResult = Invoke-RpExternalCommand -FilePath (Get-RpStackSshCommand) -ArgumentList @(
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        (Get-RpStackHost),
        $Command
    )
    $sshResult.source = "ssh"
    return $sshResult
}

function Get-RpStackToolDefinitions {
    return @(
        [ordered]@{ name = "local_revision"; description = "Read the current local Git revision and worktree status."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "server_revision"; description = "Read the Git revision currently checked out by the server-side Ansible repository."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "ansible_status"; description = "Read ansible-local-apply service status and its latest journal lines."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "compose_status"; description = "Read RP Stack Docker Compose service status."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "http_smoke"; description = "Run read-only Gateway, WorldPack, and Showroom HTTP health checks on the server."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "gateway_test"; description = "Run an isolated Gateway pytest scope in a disposable Compose container."; inputSchema = @{ type = "object"; properties = @{ scope = @{ type = "string"; enum = @("smoke", "training", "full"); default = "smoke" } }; additionalProperties = $false } },
        [ordered]@{ name = "recent_logs"; description = "Read a bounded number of recent container log lines with probable credentials redacted."; inputSchema = @{ type = "object"; properties = @{ service = @{ type = "string"; enum = @("rp-gateway", "rp-light-gui", "rp-showcase-gui"); default = "rp-gateway" }; lines = @{ type = "integer"; minimum = 1; maximum = 500; default = 100 } }; additionalProperties = $false } },
        [ordered]@{ name = "provider_summary"; description = "Read a bounded Gateway log summary for provider attempts, fallbacks, timeouts, and validation failures."; inputSchema = @{ type = "object"; properties = @{ lines = @{ type = "integer"; minimum = 1; maximum = 500; default = 100 } }; additionalProperties = $false } },
        [ordered]@{ name = "request_trace"; description = "Find bounded Gateway log lines for one validated request ID."; inputSchema = @{ type = "object"; properties = @{ request_id = @{ type = "string"; minLength = 1; maxLength = 80 }; lines = @{ type = "integer"; minimum = 1; maximum = 500; default = 100 } }; required = @("request_id"); additionalProperties = $false } },
        [ordered]@{ name = "backup_status"; description = "List recent RP Stack backup archives without reading their contents."; inputSchema = @{ type = "object"; properties = @{ lines = @{ type = "integer"; minimum = 1; maximum = 100; default = 20 } }; additionalProperties = $false } }
    )
}

function Invoke-RpStackOperation {
    param(
        [Parameter(Mandatory = $true)][string]$Action,
        [AllowNull()][object]$Arguments = $null
    )

    $supportedActions = @((Get-RpStackToolDefinitions) | ForEach-Object { $_.name })
    if ($Action -notin $supportedActions) {
        throw "Unsupported read-only operation: $Action"
    }

    $scope = [string](Get-RpArgument -Arguments $Arguments -Name "scope" -Default "smoke")
    if ($scope -notin @("smoke", "training", "full")) {
        throw "scope must be smoke, training, or full"
    }

    $lines = [int](Get-RpArgument -Arguments $Arguments -Name "lines" -Default 100)
    if ($lines -lt 1 -or $lines -gt 500) {
        throw "lines must be between 1 and 500"
    }

    $service = [string](Get-RpArgument -Arguments $Arguments -Name "service" -Default "rp-gateway")
    if ($service -notin @("rp-gateway", "rp-light-gui", "rp-showcase-gui")) {
        throw "service is not allowlisted"
    }

    if ($Action -eq "local_revision") {
        $repoRoot = Get-RpStackRepoRoot
        $commit = Invoke-RpExternalCommand -FilePath "git" -ArgumentList @("-C", $repoRoot, "rev-parse", "HEAD")
        $status = Invoke-RpExternalCommand -FilePath "git" -ArgumentList @("-C", $repoRoot, "status", "--short", "--branch")
        $combinedExit = [Math]::Max([int]$commit.exit_code, [int]$status.exit_code)
        $localOutput = "revision: {0}`n{1}" -f $commit.output, $status.output
        return [ordered]@{
            action = $Action
            ok = ($combinedExit -eq 0)
            exit_code = $combinedExit
            source = "local"
            checked_at = [DateTimeOffset]::UtcNow.ToString("o")
            output = Protect-RpStackOutput $localOutput
        }
    }

    $command = $null
    switch ($Action) {
        "server_revision" {
            $command = "git -C /opt/ubuntu_ansible_palybooks rev-parse HEAD && git -C /opt/ubuntu_ansible_palybooks status --short --branch"
        }
        "ansible_status" {
            $command = "sudo systemctl status ansible-local-apply.service --no-pager -l; sudo journalctl -u ansible-local-apply.service -n 80 --no-pager"
        }
        "compose_status" {
            $command = "cd /srv/apps/rp-stack && docker compose ps"
        }
        "http_smoke" {
            $command = "set -eu; curl -fsS http://127.0.0.1:8010/health; printf '\n'; curl -fsS http://127.0.0.1:8010/api/worldpacks >/dev/null; curl -fsS http://127.0.0.1:8011/health"
        }
        "gateway_test" {
            if ($scope -eq "full") {
                $testArgs = "pytest -q"
            } elseif ($scope -eq "training") {
                $testArgs = "pytest -q tests/test_training_runtime.py tests/test_training_capabilities.py tests/test_training_artifacts.py tests/test_awareness_one_day.py"
            } else {
                $testArgs = "pytest -q tests/test_gateway.py"
            }
            $command = "cd /srv/apps/rp-stack && docker compose run --rm rp-gateway $testArgs"
        }
        "recent_logs" {
            $command = "cd /srv/apps/rp-stack && docker compose logs --no-color --tail=$lines $service"
        }
        "provider_summary" {
            $command = "cd /srv/apps/rp-stack && docker compose logs --no-color --since=30m rp-gateway 2>&1 | grep -E -i 'provider|model_attempt|fallback|timeout|validation_failed' | tail -n $lines"
        }
        "request_trace" {
            $requestId = [string](Get-RpArgument -Arguments $Arguments -Name "request_id")
            if ([string]::IsNullOrWhiteSpace($requestId) -or $requestId -notmatch '^[A-Za-z0-9._:-]{1,80}$') {
                throw "request_id may contain only letters, digits, dot, underscore, colon, and hyphen"
            }
            $command = "cd /srv/apps/rp-stack && docker compose logs --no-color --since=24h rp-gateway 2>&1 | grep -F -- '$requestId' | tail -n $lines"
        }
        "backup_status" {
            if ($lines -gt 100) {
                throw "backup_status lines must be between 1 and 100"
            }
            $command = "find /srv/backups/rp-stack -maxdepth 1 -type f -printf '%TY-%Tm-%TdT%TH:%TM:%TSZ %s %f\n' 2>/dev/null | sort | tail -n $lines"
        }
    }

    $remoteResult = Invoke-RpStackRemote -Action $Action -Command $command
    return [ordered]@{
        action = $Action
        ok = ([int]$remoteResult.exit_code -eq 0)
        exit_code = [int]$remoteResult.exit_code
        source = $remoteResult.source
        checked_at = [DateTimeOffset]::UtcNow.ToString("o")
        output = Protect-RpStackOutput ([string]$remoteResult.output)
    }
}

Export-ModuleMember -Function Get-RpStackToolDefinitions, Invoke-RpStackOperation, Protect-RpStackOutput
