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
$activeCollectors = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'tg_archive\.cli' -and $_.CommandLine -match '\brun\b'
})
if ($activeCollectors.Count -gt 0) {
    Write-Error 'A Windows collector process is still active. Wait for it to finish before migrating the session.'
    exit 1
}
Write-Output 'Windows archive schedules disabled; no scheduled collector is running.'
