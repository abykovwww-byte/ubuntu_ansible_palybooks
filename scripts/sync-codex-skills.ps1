[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet("Check", "Apply", "Install")]
    [string]$Mode = "Check",
    [string]$DestinationRoot = (Join-Path $env:USERPROFILE ".codex\skills")
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourceRoot = Join-Path $repoRoot "codex-skills"
$skillDirectories = @(Get-ChildItem -Directory -LiteralPath $sourceRoot | Where-Object {
    Test-Path -LiteralPath (Join-Path $_.FullName "SKILL.md") -PathType Leaf
} | Sort-Object Name)

if ($skillDirectories.Count -eq 0) {
    throw "No repository skills found under $sourceRoot"
}

function Get-SkillInventory {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return @{}
    }
    $inventory = @{}
    foreach ($file in Get-ChildItem -Recurse -File -LiteralPath $Root | Sort-Object FullName) {
        $relative = $file.FullName.Substring($Root.Length + 1).Replace("\", "/")
        $inventory[$relative] = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
    }
    return $inventory
}

function Compare-SkillCopy {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $sourceInventory = Get-SkillInventory -Root $Source
    $destinationInventory = Get-SkillInventory -Root $Destination
    $differences = @()
    foreach ($relative in @($sourceInventory.Keys + $destinationInventory.Keys | Sort-Object -Unique)) {
        if (-not $sourceInventory.ContainsKey($relative)) {
            $differences += "extra installed file: $relative"
        } elseif (-not $destinationInventory.ContainsKey($relative)) {
            $differences += "missing installed file: $relative"
        } elseif ($sourceInventory[$relative] -ne $destinationInventory[$relative]) {
            $differences += "content differs: $relative"
        }
    }
    return $differences
}

if ($Mode -eq "Check") {
    $drift = @()
    foreach ($skill in $skillDirectories) {
        $destination = Join-Path $DestinationRoot $skill.Name
        foreach ($difference in Compare-SkillCopy -Source $skill.FullName -Destination $destination) {
            $drift += "$($skill.Name): $difference"
        }
    }
    if ($drift.Count -gt 0) {
        Write-Error ("Codex skill drift detected:`n- " + ($drift -join "`n- "))
        exit 1
    }
    Write-Host "Installed Codex skills match repository sources."
    exit 0
}

New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
foreach ($skill in $skillDirectories) {
    $destination = Join-Path $DestinationRoot $skill.Name
    if (-not $PSCmdlet.ShouldProcess($destination, "Replace from repository source $($skill.FullName)")) {
        continue
    }

    $stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("tavern-skill-sync-" + [Guid]::NewGuid().ToString("N"))
    $stage = Join-Path $stageRoot $skill.Name
    $backup = $destination + ".sync-backup"
    New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null
    try {
        Copy-Item -Recurse -Force -LiteralPath $skill.FullName -Destination $stage
        if (Test-Path -LiteralPath $backup) {
            throw "Refusing to overwrite stale synchronization backup: $backup"
        }
        if (Test-Path -LiteralPath $destination) {
            Move-Item -LiteralPath $destination -Destination $backup
        }
        try {
            Move-Item -LiteralPath $stage -Destination $destination
            if (Test-Path -LiteralPath $backup) {
                Remove-Item -Recurse -Force -LiteralPath $backup
            }
        } catch {
            if ((-not (Test-Path -LiteralPath $destination)) -and (Test-Path -LiteralPath $backup)) {
                Move-Item -LiteralPath $backup -Destination $destination
            }
            throw
        }
    } finally {
        if ((Test-Path -LiteralPath $stageRoot) -and
            (Resolve-Path -LiteralPath $stageRoot).Path.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -Recurse -Force -LiteralPath $stageRoot
        }
    }
    Write-Host "Installed $($skill.Name) from repository source."
}

Write-Host "Skill synchronization complete. Start a new Codex task to load the refreshed copies."
