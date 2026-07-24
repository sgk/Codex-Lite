param(
    [string]$Configuration = "Debug",
    [string]$Solution = "windows/CodexLite.sln",
    [string]$Dotnet = "C:\Program Files\dotnet\dotnet.exe",
    [switch]$Launch,
    [switch]$NoStop,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Get-ProcessAncestry {
    param([int]$ProcessId)

    $seen = @{}
    $current = $ProcessId
    while ($current -gt 0 -and -not $seen.ContainsKey($current)) {
        $seen[$current] = $true
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$current"
        if ($null -eq $proc) {
            break
        }
        [pscustomobject]@{
            ProcessId = [int]$proc.ProcessId
            ParentProcessId = [int]$proc.ParentProcessId
            Name = [string]$proc.Name
            CommandLine = [string]$proc.CommandLine
        }
        $current = [int]$proc.ParentProcessId
    }
}

function Stop-UnprotectedCodexLite {
    param([int[]]$ProtectedProcessIds)

    $protected = @{}
    foreach ($id in $ProtectedProcessIds) {
        $protected[$id] = $true
    }

    $candidates = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -eq "CodexLite.exe" -or
                ($_.Name -eq "dotnet.exe" -and $_.CommandLine -match '(?i)(^|[\\/"\s])CodexLite\.dll(?:["\s]|$)')
            }
    )
    foreach ($candidate in $candidates) {
        $candidatePid = [int]$candidate.ProcessId
        $candidateDescription = if ($candidate.Name -eq "dotnet.exe") {
            "dotnet.exe CodexLite.dll"
        } else {
            "CodexLite.exe"
        }
        if ($protected.ContainsKey($candidatePid)) {
            Write-Host "protect $candidateDescription PID $candidatePid because it is in this command's parent process chain"
            continue
        }
        if ($DryRun) {
            Write-Host "would stop $candidateDescription PID $candidatePid"
            continue
        }
        Write-Host "stop $candidateDescription PID $candidatePid"
        Stop-Process -Id $candidatePid -Force
    }
}

function Get-RepoRoot {
    $scriptDir = Split-Path -Parent $PSCommandPath
    return (Resolve-Path (Join-Path $scriptDir "..")).ProviderPath
}

$repoRoot = Get-RepoRoot
Set-Location $repoRoot

$ancestry = @(Get-ProcessAncestry -ProcessId $PID)
$protectedProcessIds = @($ancestry | ForEach-Object { [int]$_.ProcessId })
Write-Host "protected process chain:"
foreach ($proc in $ancestry) {
    Write-Host ("  {0} <- {1} {2}" -f $proc.ProcessId, $proc.ParentProcessId, $proc.Name)
}

if (-not $NoStop) {
    Stop-UnprotectedCodexLite -ProtectedProcessIds $protectedProcessIds
}

if ($DryRun) {
    exit 0
}

& $Dotnet build $Solution -c $Configuration
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if ($Launch) {
    $dllPath = Join-Path $repoRoot "windows/CodexLite/bin/$Configuration/net8.0-windows/CodexLite.dll"
    $launchedProcess = Start-Process -FilePath $Dotnet -ArgumentList @($dllPath) -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 1
    if ($launchedProcess.HasExited) {
        throw "Codex Lite exited immediately after launch with exit code $($launchedProcess.ExitCode)."
    }
    Write-Host "started dotnet.exe CodexLite.dll PID $($launchedProcess.Id)"
}
