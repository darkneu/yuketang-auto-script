# Stops only tasks launched with this project's virtual environment.
$ErrorActionPreference = 'Stop'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$pythonPrefix = '^"?' + [regex]::Escape($projectPython) + '"?\s+'
$mainArgument = '(?:^|\s)"?(?:' + [regex]::Escape((Join-Path $PSScriptRoot 'main.py')) + '|main\.py)"?(?=\s|$)'

function Get-CourseProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and
        $_.CommandLine -match $pythonPrefix -and
        $_.CommandLine -match $mainArgument
    })
}

$courseProcesses = @(Get-CourseProcesses)
if (-not $courseProcesses.Count) {
    Write-Host 'No running task found for this project.'
    exit 0
}

foreach ($courseProcess in $courseProcesses) {
    # Recheck identity before terminating, in case the task already exited.
    $currentProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($courseProcess.ProcessId)"
    if ($currentProcess -and
        $currentProcess.CreationDate -eq $courseProcess.CreationDate -and
        $currentProcess.CommandLine -match $pythonPrefix -and
        $currentProcess.CommandLine -match $mainArgument) {
        & taskkill.exe /PID $courseProcess.ProcessId /T /F
    }
}

$remainingProcesses = @(Get-CourseProcesses)
if ($remainingProcesses.Count) {
    Write-Host 'A project task is still running. Retry from the same Windows account.'
    exit 1
}
Write-Host 'Stopped. Previously submitted answers are retained on the platform.'
