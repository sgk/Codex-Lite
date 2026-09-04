param(
    [Parameter(Mandatory = $true)]
    [string]$BuildDirectory,
    [Parameter(Mandatory = $true)]
    [string]$DeploymentDirectory,
    [Parameter(Mandatory = $true)]
    [string]$DaemonEndpointPath,
    [string]$ProtectedProcessIds = "",
    [string]$LogPath = "",
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

if ($LogPath) {
    Start-Transcript -LiteralPath $LogPath -Force | Out-Null
}

function Assert-ExactChildPath {
    param(
        [string]$RuntimeDirectory,
        [string]$Candidate,
        [string]$ExpectedName
    )

    $expected = [System.IO.Path]::GetFullPath((Join-Path $RuntimeDirectory $ExpectedName))
    $resolved = [System.IO.Path]::GetFullPath($Candidate)
    if (-not [string]::Equals($resolved, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace unexpected runtime directory: $resolved"
    }
    return $resolved
}

function Stop-UnprotectedCodexLite {
    param([int[]]$ProtectedIds)

    $protected = @{}
    foreach ($protectedId in $ProtectedIds) {
        $protected[$protectedId] = $true
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
        if ($protected.ContainsKey($candidatePid)) {
            Write-Host "request graceful self-stop for Codex Lite PID $candidatePid because it is in the build command's parent process chain"
            Request-CodexLiteShutdown -ProcessId $candidatePid
            Wait-CodexLiteExit -ProcessId $candidatePid
            continue
        }
        $process = Get-Process -Id $candidatePid -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            continue
        }
        Write-Host "request graceful stop for Codex Lite PID $candidatePid"
        if (-not $process.CloseMainWindow()) {
            throw "Codex Lite PID $candidatePid did not accept a graceful stop request."
        }
        if (-not $process.WaitForExit(10000)) {
            throw "Codex Lite PID $candidatePid did not exit within 10 seconds."
        }
    }
}

function Request-CodexLiteShutdown {
    param([int]$ProcessId)

    $pipe = [System.IO.Pipes.NamedPipeClientStream]::new(".", "CodexLite-Deploy-Control", [System.IO.Pipes.PipeDirection]::Out, [System.IO.Pipes.PipeOptions]::Asynchronous)
    try {
        $pipe.Connect(3000)
        $writer = New-Object System.IO.StreamWriter($pipe)
        $writer.AutoFlush = $true
        $writer.WriteLine("restart-for-deploy")
        $writer.Dispose()
        Write-Host "requested graceful self-stop for Codex Lite PID $ProcessId"
    }
    catch {
        throw "Codex Lite PID $ProcessId is in the build process chain, but its deploy control pipe was unavailable: $($_.Exception.Message)"
    }
    finally {
        $pipe.Dispose()
    }
}

function Wait-CodexLiteExit {
    param([int]$ProcessId)

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            Write-Host "Codex Lite PID $ProcessId exited gracefully"
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Codex Lite PID $ProcessId did not exit after its graceful deploy request."
}

function Stop-RemainingLocalDaemon {
    param([string]$EndpointPath)

    if (-not (Test-Path -LiteralPath $EndpointPath -PathType Leaf)) {
        return
    }
    $endpointJson = Get-Content -LiteralPath $EndpointPath -Raw
    if ([string]::IsNullOrWhiteSpace($endpointJson)) {
        return
    }

    $endpoint = $endpointJson | ConvertFrom-Json
    if ($null -eq $endpoint.port) {
        throw "The Codex Lite daemon endpoint did not contain a port."
    }

    $shutdownUri = "http://127.0.0.1:$($endpoint.port)/shutdown"
    try {
        Invoke-RestMethod -Method Post -Uri $shutdownUri -ContentType "application/json" -Body "{}" -TimeoutSec 5 | Out-Null
        Write-Host "requested shutdown of the remaining local daemon"
    }
    catch {
        throw "The remaining Codex Lite daemon could not be stopped: $($_.Exception.Message)"
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not (Test-Path -LiteralPath $EndpointPath -PathType Leaf)) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "The remaining Codex Lite daemon did not exit within 15 seconds."
}

if (-not (Test-Path -LiteralPath (Join-Path $BuildDirectory "CodexLite.dll") -PathType Leaf)) {
    throw "Build output is incomplete: $BuildDirectory"
}

$runtimeDirectory = [System.IO.Path]::GetFullPath((Split-Path -Parent $DeploymentDirectory))
$currentDirectory = Assert-ExactChildPath -RuntimeDirectory $runtimeDirectory -Candidate $DeploymentDirectory -ExpectedName "CodexLite"
$nextDirectory = Assert-ExactChildPath -RuntimeDirectory $runtimeDirectory -Candidate (Join-Path $runtimeDirectory "CodexLite-next") -ExpectedName "CodexLite-next"
$previousDirectory = Assert-ExactChildPath -RuntimeDirectory $runtimeDirectory -Candidate (Join-Path $runtimeDirectory "CodexLite-previous") -ExpectedName "CodexLite-previous"

if (Test-Path -LiteralPath $nextDirectory) {
    Remove-Item -LiteralPath $nextDirectory -Recurse -Force
}
New-Item -ItemType Directory -Path $nextDirectory -Force | Out-Null
Copy-Item -Path (Join-Path $BuildDirectory "*") -Destination $nextDirectory -Recurse -Force
Write-Host "staged $BuildDirectory -> $nextDirectory"

$protectedIds = @(
    $ProtectedProcessIds.Split(",", [System.StringSplitOptions]::RemoveEmptyEntries) |
        ForEach-Object { [int]$_ }
)
Stop-UnprotectedCodexLite -ProtectedIds $protectedIds
Stop-RemainingLocalDaemon -EndpointPath $DaemonEndpointPath

if (Test-Path -LiteralPath $previousDirectory) {
    Remove-Item -LiteralPath $previousDirectory -Recurse -Force
}

if (Test-Path -LiteralPath $currentDirectory) {
    Move-Item -LiteralPath $currentDirectory -Destination $previousDirectory
}
Move-Item -LiteralPath $nextDirectory -Destination $currentDirectory
Write-Host "activated $currentDirectory"

$executablePath = Join-Path $currentDirectory "CodexLite.exe"
$desktopDirectory = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopDirectory "Codex Lite.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $executablePath
$shortcut.WorkingDirectory = $currentDirectory
$shortcut.IconLocation = "$executablePath,0"
$shortcut.Save()
Write-Host "updated desktop shortcut $shortcutPath"

if (-not $NoLaunch) {
    $launchedProcess = Start-Process -FilePath $executablePath -WorkingDirectory $currentDirectory -PassThru
    Start-Sleep -Milliseconds 500
    if ($launchedProcess.HasExited) {
        throw "The deployed Codex Lite process was not found after launch."
    }
    Write-Host "started deployed CodexLite.exe PID $($launchedProcess.Id)"
}

if (Test-Path -LiteralPath $previousDirectory) {
    Remove-Item -LiteralPath $previousDirectory -Recurse -Force
}

if ($LogPath) {
    Stop-Transcript | Out-Null
}
