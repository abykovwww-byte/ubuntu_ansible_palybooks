$ErrorActionPreference = "Stop"
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Import-Module (Join-Path $PSScriptRoot "RpStackOps.psm1") -Force

function Write-RpcResponse {
    param([Parameter(Mandatory = $true)][object]$Response)
    [Console]::Out.WriteLine(($Response | ConvertTo-Json -Depth 30 -Compress))
    [Console]::Out.Flush()
}

while ($null -ne ($line = [Console]::In.ReadLine())) {
    if ([string]::IsNullOrWhiteSpace($line)) {
        continue
    }

    # Windows PowerShell can preserve a UTF-8 BOM from a redirected first line.
    # MCP messages are JSON objects, so discard only transport prefix bytes before
    # the first object delimiter and leave the JSON payload untouched.
    $objectStart = $line.IndexOf('{')
    if ($objectStart -gt 0) {
        $line = $line.Substring($objectStart)
    }

    $request = $null
    try {
        $request = $line | ConvertFrom-Json
        $method = [string]$request.method
        $hasId = $request.PSObject.Properties.Name -contains "id"

        if ($method -eq "notifications/initialized" -or $method -eq "notifications/cancelled") {
            continue
        }
        if (-not $hasId) {
            continue
        }

        switch ($method) {
            "initialize" {
                $result = [ordered]@{
                    protocolVersion = "2025-06-18"
                    capabilities = @{ tools = @{ listChanged = $false } }
                    serverInfo = @{ name = "rp-stack-ops"; version = "0.1.0" }
                    instructions = "Read-only RP Stack diagnostics. No deploy, restore, delete, or live mutation tools are exposed."
                }
            }
            "ping" {
                $result = @{}
            }
            "tools/list" {
                $result = @{ tools = @(Get-RpStackToolDefinitions) }
            }
            "tools/call" {
                $toolName = [string]$request.params.name
                $arguments = $request.params.arguments
                try {
                    $operation = Invoke-RpStackOperation -Action $toolName -Arguments $arguments
                    $result = [ordered]@{
                        content = @(@{ type = "text"; text = ($operation | ConvertTo-Json -Depth 12) })
                        structuredContent = $operation
                        isError = (-not $operation.ok)
                    }
                } catch {
                    $result = [ordered]@{
                        content = @(@{ type = "text"; text = $_.Exception.Message })
                        isError = $true
                    }
                }
            }
            default {
                Write-RpcResponse ([ordered]@{
                    jsonrpc = "2.0"
                    id = $request.id
                    error = @{ code = -32601; message = "Method not found: $method" }
                })
                continue
            }
        }

        Write-RpcResponse ([ordered]@{ jsonrpc = "2.0"; id = $request.id; result = $result })
    } catch {
        $responseId = $null
        if ($null -ne $request -and $request.PSObject.Properties.Name -contains "id") {
            $responseId = $request.id
        }
        Write-RpcResponse ([ordered]@{
            jsonrpc = "2.0"
            id = $responseId
            error = @{ code = -32603; message = $_.Exception.Message }
        })
    }
}
