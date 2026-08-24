param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Offline", "SemanticAcceptance", "ProviderCanary", "BrowserReport")]
    [string]$Mode,
    [string]$BaseUrl = "",
    [string]$SourcePartyId = "",
    [string]$PlayerModelProfileId = "",
    [string]$PlayerPrompt = "Take the next in-world action only.",
    [ValidateRange(1, 5)]
    [int]$TurnCount = 1,
    [ValidateRange(30, 900)]
    [int]$TimeoutSeconds = 300,
    [switch]$ConfirmProviderRun,
    [string[]]$SemanticResponsesFile = @(),
    [string]$EvidenceFile = "",
    [string]$Output = "",
    [ValidateRange(0, 8)]
    [Nullable[int]]$RpContractRevision = $null
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $repoRoot "roles\apps\files\rp-stack\evals\run_evals.py"

function Resolve-Python {
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_PYTHON) -and (Test-Path -LiteralPath $env:CODEX_PYTHON)) {
        return $env:CODEX_PYTHON
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) {
        try {
            & $command.Source --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return $command.Source
            }
        } catch {
        }
    }
    $bundled = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $bundled) {
        return $bundled
    }
    throw "Python 3 is required. Set CODEX_PYTHON to its executable path."
}

$python = Resolve-Python
$arguments = @($runner)
switch ($Mode) {
    "Offline" {
        $arguments += "offline"
    }
    "SemanticAcceptance" {
        $arguments += "semantic-acceptance"
    }
    "ProviderCanary" {
        if (-not $ConfirmProviderRun) {
            throw "Provider canary requires -ConfirmProviderRun."
        }
        foreach ($requiredValue in @($BaseUrl, $SourcePartyId, $PlayerModelProfileId, $PlayerPrompt)) {
            if ([string]::IsNullOrWhiteSpace($requiredValue)) {
                throw "BaseUrl, SourcePartyId, PlayerModelProfileId, and PlayerPrompt are required."
            }
        }
        if ($SemanticResponsesFile.Count -lt 1) {
            throw "ProviderCanary requires at least one -SemanticResponsesFile captured from a provider-canary run."
        }
        $arguments += @(
            "provider-canary",
            "--base-url", $BaseUrl,
            "--source-party-id", $SourcePartyId,
            "--player-model-profile-id", $PlayerModelProfileId,
            "--player-prompt", $PlayerPrompt,
            "--turn-count", [string]$TurnCount,
            "--timeout-seconds", [string]$TimeoutSeconds,
            "--confirm-provider-run"
        )
        if ($null -ne $RpContractRevision) {
            $arguments += @("--rp-contract-revision", [string]$RpContractRevision)
        }
        foreach ($semanticResponses in $SemanticResponsesFile) {
            if (-not (Test-Path -LiteralPath $semanticResponses -PathType Leaf)) {
                throw "SemanticResponsesFile does not exist: $semanticResponses"
            }
            $arguments += @("--semantic-responses", $semanticResponses)
        }
    }
    "BrowserReport" {
        if ([string]::IsNullOrWhiteSpace($EvidenceFile)) {
            throw "BrowserReport requires -EvidenceFile."
        }
        $arguments += @("browser-report", "--evidence-file", $EvidenceFile)
    }
}
if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $arguments += @("--output", $Output)
}

& $python @arguments
exit $LASTEXITCODE
