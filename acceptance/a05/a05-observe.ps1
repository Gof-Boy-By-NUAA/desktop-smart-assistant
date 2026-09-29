#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$deadline = [DateTime]::UtcNow.AddSeconds(30)
$lastElectron = $null
$lastBackend = $null
$lastListener = $null

do {
    $processes = @(Get-CimInstance -ClassName Win32_Process)
    $electronProcesses = @(
        $processes |
            Where-Object { $_.Name -ieq 'SmartAssistant.exe' }
    )
    $electronIds = @($electronProcesses | ForEach-Object { [uint32]$_.ProcessId })
    $backendProcesses = @(
        $processes |
            Where-Object {
                $_.Name -ieq 'smart-assistant-backend.exe' -and
                $electronIds -contains [uint32]$_.ParentProcessId
            }
    )

    $lastElectron = $electronProcesses | Sort-Object ProcessId | Select-Object -First 1
    $lastBackend = $backendProcesses | Sort-Object ProcessId | Select-Object -First 1
    $lastListener = $null

    if ($null -ne $lastBackend) {
        $lastListener = @(
            Get-NetTCPConnection -State Listen -OwningProcess $lastBackend.ProcessId -ErrorAction SilentlyContinue |
                Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') } |
                Sort-Object LocalPort
        ) | Select-Object -First 1
    }

    if ($null -ne $lastElectron -and $null -ne $lastBackend -and $null -ne $lastListener) {
        Write-Output "ELECTRON_PID=$($lastElectron.ProcessId)"
        Write-Output "BACKEND_PID=$($lastBackend.ProcessId)"
        Write-Output "BACKEND_LISTENER=$($lastListener.LocalAddress):$($lastListener.LocalPort)"
        Write-Output 'OBSERVE_DONE'
        exit 0
    }

    Write-Output 'STARTUP_PENDING'
    $remainingMilliseconds = [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalMilliseconds)
    if ($remainingMilliseconds -le 0) {
        break
    }
    Start-Sleep -Milliseconds ([Math]::Min(2000, $remainingMilliseconds))
} while ([DateTime]::UtcNow -lt $deadline)

if ($null -eq $lastElectron) {
    Write-Error 'ELECTRON_NOT_FOUND'
} elseif ($null -eq $lastBackend) {
    Write-Error 'BACKEND_PROCESS_NOT_FOUND'
} else {
    Write-Error 'BACKEND_LISTENER_NOT_FOUND'
}
exit 1
