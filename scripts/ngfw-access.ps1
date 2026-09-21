[CmdletBinding()]
param(
    [ValidateSet('Check', 'Ssh', 'Proxy', 'TestMonitor')]
    [string]$Action = 'Check',
    [ValidateSet('Auto', 'Local', 'Tailscale')]
    [string]$Transport = 'Auto',
    [string]$IdentityFile = (Join-Path $env:USERPROFILE '.ssh/id_ed25519'),
    [ValidateRange(1024, 65535)]
    [int]$ProxyPort = 1080,
    [ValidateRange(1024, 65535)]
    [int]$MonitorPort = 8787
)

# No SSH config, credentials, certificates, routes or server settings are changed.
$ErrorActionPreference = 'Stop'
$sshExe = Join-Path $env:WINDIR 'System32/OpenSSH/ssh.exe'
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw 'Pass -IdentityFile with your existing authorized workstation key.'
}
$endpoints = @{
    Local = '192.168.1.88'
    Tailscale = '100.117.52.16'
}
$order = if ($Transport -eq 'Auto') { @('Local', 'Tailscale') } else { @($Transport) }
$common = @('-i', $IdentityFile, '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=3')
$selected = $null
foreach ($candidate in $order) {
    # Authentication and known_hosts verification, not merely an open TCP port.
    $target = 'abykov@' + $endpoints[$candidate]
    # Windows PowerShell 5.1 can turn native stderr into a terminating error.
    # A failed path must not prevent trying the second path.
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $probe = & $sshExe @common -o BatchMode=yes $target hostname 2>$null
        $probeExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($probeExit -eq 0 -and ($probe -join '').Trim() -eq 'abykovserv') {
        $selected = $candidate
        break
    }
}
if (-not $selected) {
    throw 'No verified path to abykovserv. Check VPN, authorized key and known_hosts; do not disable host-key checking.'
}
$target = 'abykov@' + $endpoints[$selected]
Write-Host ('Verified {0} access: {1}' -f $selected, $target)
switch ($Action) {
    'Check' { exit 0 }
    'Ssh' {
        & $sshExe @common $target
        exit $LASTEXITCODE
    }
    'Proxy' {
        Write-Host ('SOCKS5: 127.0.0.1:{0}. Keep this terminal open; Ctrl+C stops the tunnel.' -f $ProxyPort)
        Write-Host 'Use the original HTTPS URL https://192.168.1.88:8443/ through this proxy; certificate validation remains enabled.'
        & $sshExe @common -o BatchMode=yes -o ExitOnForwardFailure=yes `
            -o ServerAliveInterval=15 -o ServerAliveCountMax=3 `
            -N -D ('127.0.0.1:' + $ProxyPort) $target
        exit $LASTEXITCODE
    }
    'TestMonitor' {
        Write-Host ('Read-only test monitor: http://127.0.0.1:{0}/. Keep this tunnel open.' -f $MonitorPort)
        Write-Host 'Requires monitor.py already running on server loopback port 8787; does not start traffic or services.'
        & $sshExe @common -o BatchMode=yes -o ExitOnForwardFailure=yes `
            -o ServerAliveInterval=15 -o ServerAliveCountMax=3 `
            -N -L ('127.0.0.1:' + $MonitorPort + ':127.0.0.1:8787') $target
        exit $LASTEXITCODE
    }
}
