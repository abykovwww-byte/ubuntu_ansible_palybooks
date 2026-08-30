Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:RegisteredCausalExpectations = @(
    "seed_trust_influences_plot",
    "relationship_pressure_reaches_next_turn_prompt",
    "relationship_event_has_canonical_character_attribution",
    "relationship_badge_has_canonical_character_attribution",
    "trust_gained_reaches_next_turn_prompt"
)

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
    $property = $Arguments.PSObject.Properties[$Name]
    if ($null -ne $property) {
        return $property.Value
    }
    return $Default
}

function Protect-RpStackSecrets {
    param([AllowNull()][string]$Text)

    if ($null -eq $Text) {
        return ""
    }
    $redacted = $Text
    $redacted = [regex]::Replace($redacted, '(?i)\bAuthorization\b[''"]?\s*[:=]\s*(?:Bearer\s+)?[^\s,;}\)]+', 'Authorization=[REDACTED]')
    $redacted = [regex]::Replace($redacted, '(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*', 'Bearer [REDACTED]')
    $redacted = [regex]::Replace($redacted, '(?i)\bsk-[A-Za-z0-9_-]{8,}\b', '[REDACTED_API_KEY]')
    $redacted = [regex]::Replace(
        $redacted,
        '(?im)\b(api[_-]?key|cookie|password(?:[_-]?hash)?|secret|token)\b[''"]?\s*[:=]\s*[''"]?[^\s,;}\)]+',
        '$1=[REDACTED]'
    )
    return $redacted
}

function Protect-RpStackOutput {
    param([AllowNull()][string]$Text)

    $redacted = Protect-RpStackSecrets $Text
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

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Native tools such as Docker may write progress messages to stderr even
        # when they exit successfully. Capture those lines without promoting them
        # to terminating PowerShell errors so the native exit code remains authoritative.
        $ErrorActionPreference = "Continue"
        $lines = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object { [string]$_ })
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
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
        $fixture = Join-Path $env:RP_STACK_OPS_FIXTURE_DIR ($Action + ".json")
        if (-not (Test-Path -LiteralPath $fixture -PathType Leaf)) {
            $fixture = Join-Path $env:RP_STACK_OPS_FIXTURE_DIR ($Action + ".txt")
        }
        if (-not (Test-Path -LiteralPath $fixture -PathType Leaf)) {
            throw "Missing RP Stack ops fixture for action: $Action"
        }
        return [ordered]@{
            exit_code = 0
            output = Get-Content -Raw -LiteralPath $fixture
            source = "fixture"
        }
    }

    $sshArguments = @(
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10"
    )
    $identityFile = $env:RP_STACK_OPS_IDENTITY_FILE
    if ([string]::IsNullOrWhiteSpace($identityFile)) {
        $defaultIdentityFile = Join-Path $env:USERPROFILE ".ssh\id_ed25519_codex_abykovserv"
        if (Test-Path -LiteralPath $defaultIdentityFile -PathType Leaf) {
            $identityFile = $defaultIdentityFile
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($identityFile)) {
        if (-not (Test-Path -LiteralPath $identityFile -PathType Leaf)) {
            throw "RP_STACK_OPS_IDENTITY_FILE must point to an existing file."
        }
        $resolvedIdentityFile = (Resolve-Path -LiteralPath $identityFile).Path
        $sshArguments += @("-i", $resolvedIdentityFile, "-o", "IdentitiesOnly=yes")
    }
    $sshArguments += @((Get-RpStackHost), $Command)

    $sshResult = Invoke-RpExternalCommand -FilePath (Get-RpStackSshCommand) -ArgumentList $sshArguments
    $sshResult.source = "ssh"
    return $sshResult
}

function Test-RpStackInteger {
    param([AllowNull()][object]$Value)

    return $Value -is [byte] -or $Value -is [int16] -or $Value -is [int32] -or $Value -is [int64]
}

function Get-RpProbePython {
    param([Parameter(Mandatory = $true)][string]$Action)

    switch ($Action) {
        "loop_probe" {
            return @'
import json
import sqlite3
import sys

party_id = sys.argv[1]
db = sqlite3.connect("file:/data/rp_gateway.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

party = db.execute("SELECT 1 FROM campaigns WHERE id = ?", (party_id,)).fetchone()
if party is None:
    print(json.dumps({"ok": False, "error_code": "party_not_found", "party_id": party_id}))
    raise SystemExit(0)

counters = {}
operations_total = 0
operations_outside_timeline = 0
for row in db.execute(
    "SELECT patch_json FROM state_patches WHERE campaign_id = ? AND applied = 1 ORDER BY id",
    (party_id,),
):
    try:
        patch = json.loads(row["patch_json"] or "{}")
    except (TypeError, ValueError):
        continue
    for operation in patch.get("patch", []):
        if not isinstance(operation, dict):
            continue
        operation_type = str(operation.get("op") or "unknown")
        counters[operation_type] = counters.get(operation_type, 0) + 1
        operations_total += 1
        path = str(operation.get("path") or "")
        if path != "/timeline" and not path.startswith("/timeline/"):
            operations_outside_timeline += 1

latest_by_turn = {}
for row in db.execute(
    "SELECT id, event_type, event_json FROM audit_events "
    "WHERE campaign_id = ? AND event_type IN "
    "('relationship_extraction_applied','relationship_extraction_rejected','relationship_extraction_failed') "
    "ORDER BY id",
    (party_id,),
):
    try:
        payload = json.loads(row["event_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, int) and not isinstance(turn_id, bool):
        latest_by_turn[turn_id] = (row["event_type"], payload)

observed = len(latest_by_turn)
nonempty = sum(
    1
    for event_type, payload in latest_by_turn.values()
    if event_type == "relationship_extraction_applied" and int(payload.get("extracted_events") or 0) > 0
)
share = round(nonempty / observed, 6) if observed else 0.0
print(json.dumps({
    "ok": True,
    "party_id": party_id,
    "necessary_not_sufficient": True,
    "operation_counters": dict(sorted(counters.items())),
    "operations_total": operations_total,
    "operations_outside_timeline": operations_outside_timeline,
    "nonempty_extraction_share": {
        "nonempty_turns": nonempty,
        "observed_turns": observed,
        "share": share,
    },
}, ensure_ascii=False, sort_keys=True))
'@
        }
        "causal_probe" {
            return @'
import json
import sqlite3
import sys

party_id = sys.argv[1]
expectation = sys.argv[2]
db = sqlite3.connect("file:/data/rp_gateway.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

party = db.execute("SELECT 1 FROM campaigns WHERE id = ?", (party_id,)).fetchone()
if party is None:
    print(json.dumps({"ok": False, "error_code": "party_not_found", "party_id": party_id, "expectation": expectation}))
    raise SystemExit(0)

canonical_names = {}
seeded = {}
initial = db.execute(
    "SELECT state_json FROM state_versions WHERE campaign_id = ? ORDER BY version ASC LIMIT 1",
    (party_id,),
).fetchone()
if initial is not None:
    try:
        state = json.loads(initial["state_json"] or "{}")
    except (TypeError, ValueError):
        state = {}
    characters = state.get("characters") if isinstance(state, dict) else None
    if isinstance(characters, dict):
        for character_id, character in characters.items():
            if not isinstance(character, dict):
                continue
            canonical_names[str(character_id)] = str(character.get("name") or character_id)
            trust = character.get("trust")
            if isinstance(trust, int) and not isinstance(trust, bool) and trust != 0:
                seeded[str(character_id)] = str(character.get("name") or character_id)
    relationships = state.get("relationships") if isinstance(state, dict) else None
    if isinstance(relationships, dict):
        for relationship in relationships.values():
            if not isinstance(relationship, dict):
                continue
            trust = relationship.get("trust")
            endpoints = {relationship.get("from"), relationship.get("to")}
            if not isinstance(trust, int) or isinstance(trust, bool) or trust == 0 or "player" not in endpoints:
                continue
            for endpoint in endpoints - {"player", None}:
                character_id = str(endpoint)
                seeded.setdefault(character_id, character_id)

def walk_strings(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_strings(child)
    elif isinstance(value, str):
        yield value

def pressure_match(character_id, after_party_turn):
    character_name = canonical_names.get(character_id)
    if not character_name:
        return None
    rows = db.execute(
        "SELECT id, party_turn, prompt_json FROM turns "
        "WHERE campaign_id = ? AND party_turn > ? AND prompt_json IS NOT NULL ORDER BY party_turn, id",
        (party_id, int(after_party_turn)),
    )
    for row in rows:
        try:
            prompt = json.loads(row["prompt_json"] or "null")
        except (TypeError, ValueError):
            continue
        blocks = [
            text for text in walk_strings(prompt)
            if text.startswith("RELATIONSHIP_PRESSURE") and character_name in text
        ]
        if blocks:
            return {
                "turn_id": int(row["id"]),
                "party_turn": int(row["party_turn"]),
                "quote": blocks[0][:160].replace("\n", " "),
            }
    return None

def first_pressure_match(rows, turn_key):
    for row in rows:
        character_id = str(row["character_id"])
        if character_id not in canonical_names:
            continue
        match = pressure_match(character_id, int(row[turn_key]))
        if match is not None:
            return match
    return None

def finish(assertion, steps):
    break_at = next((step["step"] for step in steps if not step["passed"]), None)
    print(json.dumps({
        "ok": True,
        "party_id": party_id,
        "expectation": expectation,
        "assertion": assertion,
        "passed": break_at is None,
        "break_at": break_at,
        "steps": steps,
    }, ensure_ascii=False, sort_keys=True))

if expectation == "seed_trust_influences_plot":
    seed_cause_characters = set()
    for row in db.execute(
        "SELECT character_id, weight FROM relationship_causes "
        "WHERE campaign_id = ? AND event_id = 'seed_trust' AND source = 'seed'",
        (party_id,),
    ):
        if int(row["weight"]) != 0:
            character_id = str(row["character_id"])
            seed_cause_characters.add(character_id)
            seeded.setdefault(character_id, canonical_names.get(character_id, character_id))
    events = [
        dict(row) for row in db.execute(
            "SELECT character_id, event_id, opened_turn FROM narrative_events "
            "WHERE campaign_id = ? ORDER BY opened_turn, id",
            (party_id,),
        ) if str(row["character_id"]) in seed_cause_characters
    ]
    prompt = first_pressure_match(events, "opened_turn")
    finish("prompt_presence", [
        {"step": "seeded_trust", "assertion": "state_change", "passed": bool(seeded),
         "evidence": {"seeded_characters": len(seeded), "character_ids": sorted(seeded)}},
        {"step": "event_projection", "assertion": "projection", "passed": bool(events),
         "evidence": {"seed_causes": len(seed_cause_characters), "events": len(events),
                      "first_opened_turn": int(events[0]["opened_turn"]) if events else None}},
        {"step": "prompt_presence", "assertion": "prompt_presence", "passed": prompt is not None,
         "evidence": prompt or {"matching_turns": 0}},
    ])

elif expectation == "relationship_pressure_reaches_next_turn_prompt":
    causes = [dict(row) for row in db.execute(
        "SELECT character_id, event_id, party_turn, source FROM relationship_causes "
        "WHERE campaign_id = ? AND weight != 0 AND source != 'seed' ORDER BY party_turn, id",
        (party_id,),
    )]
    invalid = sorted({str(row["character_id"]) for row in causes if str(row["character_id"]) not in canonical_names})
    prompt = first_pressure_match(causes, "party_turn")
    finish("prompt_presence", [
        {"step": "relationship_cause", "assertion": "state_change", "passed": bool(causes),
         "evidence": {"causes": len(causes), "event_ids": sorted({str(row["event_id"]) for row in causes})}},
        {"step": "canonical_character_attribution", "assertion": "projection",
         "passed": bool(causes) and not invalid,
         "evidence": {"canonical_causes": len(causes) - len(invalid), "invalid_character_ids": invalid}},
        {"step": "prompt_presence", "assertion": "prompt_presence", "passed": prompt is not None,
         "evidence": prompt or {"matching_turns": 0}},
    ])

elif expectation == "relationship_event_has_canonical_character_attribution":
    extracted = [dict(row) for row in db.execute(
        "SELECT character_id, event_id, party_turn FROM relationship_causes "
        "WHERE campaign_id = ? AND source = 'extraction' ORDER BY party_turn, id",
        (party_id,),
    )]
    invalid = sorted({str(row["character_id"]) for row in extracted if str(row["character_id"]) not in canonical_names})
    finish("projection", [
        {"step": "relationship_extraction", "assertion": "state_change", "passed": bool(extracted),
         "evidence": {"events": len(extracted), "event_ids": sorted({str(row["event_id"]) for row in extracted})}},
        {"step": "canonical_character_attribution", "assertion": "projection",
         "passed": bool(extracted) and not invalid,
         "evidence": {"attributed_events": len(extracted) - len(invalid), "invalid_character_ids": invalid}},
    ])

elif expectation == "relationship_badge_has_canonical_character_attribution":
    badges = [dict(row) for row in db.execute(
        "SELECT character_id, badge_kind, badge_id, party_turn FROM character_badges "
        "WHERE campaign_id = ? AND active = 1 ORDER BY party_turn, id",
        (party_id,),
    )]
    invalid = sorted({str(row["character_id"]) for row in badges if str(row["character_id"]) not in canonical_names})
    finish("projection", [
        {"step": "badge_projection", "assertion": "state_change", "passed": bool(badges),
         "evidence": {"badges": len(badges), "badge_kinds": sorted({str(row["badge_kind"]) for row in badges})}},
        {"step": "canonical_character_attribution", "assertion": "projection",
         "passed": bool(badges) and not invalid,
         "evidence": {"attributed_badges": len(badges) - len(invalid), "invalid_character_ids": invalid}},
    ])

elif expectation == "trust_gained_reaches_next_turn_prompt":
    gained = [dict(row) for row in db.execute(
        "SELECT character_id, event_id, party_turn FROM relationship_causes "
        "WHERE campaign_id = ? AND event_id = 'trust_gained' AND weight > 0 ORDER BY party_turn, id",
        (party_id,),
    )]
    invalid = sorted({str(row["character_id"]) for row in gained if str(row["character_id"]) not in canonical_names})
    prompt = first_pressure_match(gained, "party_turn")
    finish("prompt_presence", [
        {"step": "trust_gained_projection", "assertion": "state_change", "passed": bool(gained),
         "evidence": {"events": len(gained)}},
        {"step": "canonical_character_attribution", "assertion": "projection",
         "passed": bool(gained) and not invalid,
         "evidence": {"attributed_events": len(gained) - len(invalid), "invalid_character_ids": invalid}},
        {"step": "prompt_presence", "assertion": "prompt_presence", "passed": prompt is not None,
         "evidence": prompt or {"matching_turns": 0}},
    ])

else:
    print(json.dumps({"ok": False, "error_code": "expectation_not_registered", "expectation": expectation}))
'@
        }
        "service_llm_trace" {
            return @'
import json
import re
import sqlite3
import sys

party_id = sys.argv[1]
turn = int(sys.argv[2])
db = sqlite3.connect("file:/data/rp_gateway.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

table = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'service_call_log'").fetchone()
if table is None:
    print(json.dumps({"ok": False, "error_code": "service_call_log_missing", "party_id": party_id, "turn": turn}))
    raise SystemExit(0)

party = db.execute("SELECT 1 FROM campaigns WHERE id = ?", (party_id,)).fetchone()
if party is None:
    print(json.dumps({"ok": False, "error_code": "party_not_found", "party_id": party_id, "turn": turn}))
    raise SystemExit(0)

def redact(text):
    value = "" if text is None else str(text)
    value = re.sub(r"(?i)\bAuthorization\b['\"]?\s*[:=]\s*(?:Bearer\s+)?[^\s,;}\)]+", "Authorization=[REDACTED]", value)
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_API_KEY]", value)
    value = re.sub(
        r"(?im)\b(api[_-]?key|cookie|password(?:[_-]?hash)?|secret|token)\b['\"]?\s*[:=]\s*['\"]?[^\s,;}\)]+",
        r"\1=[REDACTED]",
        value,
    )
    return value

records = []
for row in db.execute(
    "SELECT party_id, turn_id, role, prompt_text, raw_response, created_at, status "
    "FROM service_call_log WHERE party_id = ? AND turn_id = ? ORDER BY created_at, rowid",
    (party_id, turn),
):
    records.append({
        "party_id": str(row["party_id"]),
        "turn_id": int(row["turn_id"]),
        "role": str(row["role"]),
        "prompt_text": redact(row["prompt_text"]),
        "raw_response": redact(row["raw_response"]),
        "created_at": row["created_at"],
        "status": str(row["status"]),
    })
print(json.dumps({"ok": True, "party_id": party_id, "turn": turn, "records": records}, ensure_ascii=False))
'@
        }
        default {
            throw "Unsupported SQLite probe: $Action"
        }
    }
}

function New-RpProbeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Action,
        [Parameter(Mandatory = $true)][string]$PartyId,
        [AllowNull()][string]$Expectation = $null,
        [AllowNull()][object]$Turn = $null
    )

    $script = Get-RpProbePython -Action $Action
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
    $arguments = @($PartyId)
    if (-not [string]::IsNullOrWhiteSpace($Expectation)) {
        $arguments += $Expectation
    }
    if ($null -ne $Turn) {
        $arguments += [string]$Turn
    }
    return "printf '%s' '$encoded' | base64 -d | docker exec -i rp-stack-gateway python - " + ($arguments -join " ")
}

function ConvertFrom-RpProbeResult {
    param(
        [Parameter(Mandatory = $true)][string]$Action,
        [Parameter(Mandatory = $true)][object]$RemoteResult
    )

    $result = [ordered]@{
        action = $Action
        ok = $false
        exit_code = [int]$RemoteResult.exit_code
        source = $RemoteResult.source
        checked_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    if ([int]$RemoteResult.exit_code -ne 0) {
        $result.output = Protect-RpStackOutput ([string]$RemoteResult.output)
        return $result
    }
    try {
        $payload = [string]$RemoteResult.output | ConvertFrom-Json
    } catch {
        $result.error_code = "invalid_probe_output"
        $result.output = Protect-RpStackOutput ([string]$RemoteResult.output)
        return $result
    }
    if ($Action -eq "service_llm_trace") {
        $recordsProperty = $payload.PSObject.Properties["records"]
        if ($null -ne $recordsProperty) {
            foreach ($record in @($recordsProperty.Value)) {
                $record.prompt_text = Protect-RpStackSecrets ([string]$record.prompt_text)
                $record.raw_response = Protect-RpStackSecrets ([string]$record.raw_response)
            }
        }
    }
    foreach ($property in $payload.PSObject.Properties) {
        $result[$property.Name] = $property.Value
    }
    $result.ok = [bool]$payload.ok
    return $result
}

function Get-RpStackToolDefinitions {
    return @(
        [ordered]@{ name = "local_revision"; description = "Read the current local Git revision and worktree status."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "server_revision"; description = "Read the Git revision currently checked out by the server-side Ansible repository."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "ansible_status"; description = "Read ansible-local-apply service status and its latest journal lines."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "compose_status"; description = "Read RP Stack Docker Compose service status."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "http_smoke"; description = "Run read-only Gateway, WorldPack, and Showroom HTTP health checks on the server."; inputSchema = @{ type = "object"; properties = @{}; additionalProperties = $false } },
        [ordered]@{ name = "gateway_test"; description = "Run an isolated RP Gateway pytest scope in a disposable Compose container."; inputSchema = @{ type = "object"; properties = @{ scope = @{ type = "string"; enum = @("smoke", "full"); default = "smoke" } }; additionalProperties = $false } },
        [ordered]@{ name = "recent_logs"; description = "Read a bounded number of recent RP container log lines with probable credentials redacted."; inputSchema = @{ type = "object"; properties = @{ service = @{ type = "string"; enum = @("rp-gateway", "rp-light-gui"); default = "rp-gateway" }; lines = @{ type = "integer"; minimum = 1; maximum = 500; default = 100 } }; additionalProperties = $false } },
        [ordered]@{ name = "provider_summary"; description = "Read a bounded Gateway log summary for provider attempts, fallbacks, timeouts, and validation failures."; inputSchema = @{ type = "object"; properties = @{ lines = @{ type = "integer"; minimum = 1; maximum = 500; default = 100 } }; additionalProperties = $false } },
        [ordered]@{ name = "request_trace"; description = "Find bounded Gateway log lines for one validated request ID."; inputSchema = @{ type = "object"; properties = @{ request_id = @{ type = "string"; minLength = 1; maxLength = 80 }; lines = @{ type = "integer"; minimum = 1; maximum = 500; default = 100 } }; required = @("request_id"); additionalProperties = $false } },
        [ordered]@{ name = "loop_probe"; description = "Read diagnostic party-loop counters; these counters are necessary but not sufficient evidence."; inputSchema = @{ type = "object"; properties = @{ party_id = @{ type = "string"; minLength = 1; maxLength = 80; pattern = "^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$" } }; required = @("party_id"); additionalProperties = $false } },
        [ordered]@{ name = "causal_probe"; description = "Check each registered causal-chain step for a party without exposing narrative beyond a short quote."; inputSchema = @{ type = "object"; properties = @{ party_id = @{ type = "string"; minLength = 1; maxLength = 80; pattern = "^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$" }; expectation = @{ type = "string"; enum = @($script:RegisteredCausalExpectations) } }; required = @("party_id", "expectation"); additionalProperties = $false } },
        [ordered]@{ name = "service_llm_trace"; description = "Read exact redacted service-model prompts and raw responses for one party turn."; inputSchema = @{ type = "object"; properties = @{ party_id = @{ type = "string"; minLength = 1; maxLength = 80; pattern = "^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$" }; turn = @{ type = "integer"; minimum = 1 } }; required = @("party_id", "turn"); additionalProperties = $false } },
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
    if ($scope -notin @("smoke", "full")) {
        throw "scope must be smoke or full"
    }

    $lines = [int](Get-RpArgument -Arguments $Arguments -Name "lines" -Default 100)
    if ($lines -lt 1 -or $lines -gt 500) {
        throw "lines must be between 1 and 500"
    }

    $service = [string](Get-RpArgument -Arguments $Arguments -Name "service" -Default "rp-gateway")
    if ($service -notin @("rp-gateway", "rp-light-gui")) {
        throw "service is not allowlisted"
    }

    $probeActions = @("loop_probe", "causal_probe", "service_llm_trace")
    $partyIdValue = Get-RpArgument -Arguments $Arguments -Name "party_id"
    $partyId = if ($partyIdValue -is [string]) { $partyIdValue } else { "" }
    if ($Action -in $probeActions -and (
        [string]::IsNullOrWhiteSpace($partyId) -or $partyId -notmatch '^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$'
    )) {
        throw "party_id may contain only letters, digits, dot, underscore, colon, and hyphen"
    }
    $expectationValue = Get-RpArgument -Arguments $Arguments -Name "expectation"
    $expectation = if ($expectationValue -is [string]) { $expectationValue } else { "" }
    if ($Action -eq "causal_probe" -and $expectation -cnotin $script:RegisteredCausalExpectations) {
        throw "expectation must be a registered expectation: $($script:RegisteredCausalExpectations -join ', ')"
    }
    $turn = Get-RpArgument -Arguments $Arguments -Name "turn"
    if ($Action -eq "service_llm_trace" -and (-not (Test-RpStackInteger $turn) -or [int64]$turn -lt 1)) {
        throw "turn must be an integer greater than or equal to 1"
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
            $command = "systemctl status ansible-local-apply.service --no-pager -l; journalctl -u ansible-local-apply.service -n 80 --no-pager"
        }
        "compose_status" {
            $command = "cd /srv/apps/rp-stack && docker compose ps"
        }
        "http_smoke" {
            $command = "set -eu; curl -fsS http://192.168.1.88:8010/health; printf '\n'; curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.1.88:8010/api/worldpacks | grep -qx 401; printf 'worldpacks_unauth=401\n'; curl -fsS http://192.168.1.88:8011/health; printf '\n'; curl -fsS http://192.168.1.88:8011/api/showroom/scenarios >/dev/null; printf 'showroom_scenarios=200\n'"
        }
        "gateway_test" {
            if ($scope -eq "full") {
                $testArgs = "pytest -q"
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
        "loop_probe" {
            $command = New-RpProbeCommand -Action $Action -PartyId $partyId
        }
        "causal_probe" {
            $command = New-RpProbeCommand -Action $Action -PartyId $partyId -Expectation $expectation
        }
        "service_llm_trace" {
            $command = New-RpProbeCommand -Action $Action -PartyId $partyId -Turn ([int64]$turn)
        }
        "backup_status" {
            if ($lines -gt 100) {
                throw "backup_status lines must be between 1 and 100"
            }
            $command = "find /srv/backups/rp-stack -maxdepth 1 -type f -printf '%TY-%Tm-%TdT%TH:%TM:%TSZ %s %f\n' 2>/dev/null | sort | tail -n $lines"
        }
    }

    $remoteResult = Invoke-RpStackRemote -Action $Action -Command $command
    if ($Action -in $probeActions) {
        return ConvertFrom-RpProbeResult -Action $Action -RemoteResult $remoteResult
    }
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
