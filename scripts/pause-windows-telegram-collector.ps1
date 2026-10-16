# Run before starting the server with the migrated Telegram session.
# Keep disabled definitions for rollback until the first verified server ok run.
$ErrorActionPreference = 'Stop'
$archiveTasks = @('Telegram Archive daily', 'Telegram Archive work hours')
foreach ($archiveTask in $archiveTasks) {
    Disable-ScheduledTask -TaskName $archiveTask | Out-Null
}
$runningTasks = @($archiveTasks | ForEach-Object { Get-ScheduledTask -TaskName $_ } | Where-Object { $_.State -eq 'Running' })
if ($runningTasks.Count -gt 0) {
    Write-Error 'Windows collection is still running. Wait for completion before starting the server collector.'
    exit 1
}
Write-Output 'Windows archive schedules disabled; no scheduled collector is running.'
