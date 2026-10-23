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
        # Send one complete frame. StreamWriter.AutoFlush plus Dispose can
        # flush again after the server has read the command and closed its
        # pipe, incorrectly reporting a broken pipe for accepted shutdowns.
        $commandBytes = [System.Text.Encoding]::UTF8.GetBytes("restart-for-deploy`n")
        $pipe.Write($commandBytes, 0, $commandBytes.Length)
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

    # Shutdown serializes with daemon initialization (up to 180 seconds).
    # Allow that operation to finish without killing the parent process tree.
    $deadline = [DateTime]::UtcNow.AddSeconds(240)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            Write-Host "Codex Lite PID $ProcessId exited gracefully"
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Codex Lite PID $ProcessId did not exit after its graceful deploy request."
}

function Get-CodexLiteWslChildren {
    # wslhost can retain the inherited Windows working directory after the
    # client window closes. Observe only this app's descendants, never stop
    # unrelated WSL sessions or restart the distribution.
    $snapshot = @(Get-CimInstance Win32_Process)
    $owners = @{}
    foreach ($item in $snapshot) {
        if ($item.Name -eq "CodexLite.exe") {
            $owners[[int]$item.ProcessId] = $item.CreationDate
        }
    }
    do {
        $changed = $false
        foreach ($item in $snapshot) {
            # ParentProcessId can refer to a newer, unrelated process after PID
            # reuse. A real descendant cannot predate its observed parent.
            if ($owners.ContainsKey([int]$item.ParentProcessId) -and
                $null -ne $item.CreationDate -and
                $null -ne $owners[[int]$item.ParentProcessId] -and
                $item.CreationDate -ge $owners[[int]$item.ParentProcessId] -and
                -not $owners.ContainsKey([int]$item.ProcessId)) {
                $owners[[int]$item.ProcessId] = $item.CreationDate
                $changed = $true
            }
        }
    } while ($changed)
    return @($snapshot | Where-Object {
        $owners.ContainsKey([int]$_.ProcessId) -and $_.Name -in @("wsl.exe", "wslhost.exe")
    })
}

function Wait-CodexLiteWslChildrenExit {
    param([object[]]$Children)

    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $remaining = @($Children | Where-Object {
            $observed = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.ProcessId)"
            $null -ne $observed -and $observed.CreationDate -eq $_.CreationDate
        })
        if ($remaining.Count -eq 0) {
            Write-Host "Codex Lite WSL children exited; deployment directory is no longer their working directory"
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Codex Lite WSL children still own the deployment directory after 60 seconds: $($remaining.ProcessId -join ',')"
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
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    & $curl --silent --output NUL --max-time 5 --request POST --header "Content-Type: application/json" --data "{}" $shutdownUri
    $shutdownResponseReceived = $LASTEXITCODE -eq 0
    if ($shutdownResponseReceived) {
        Write-Host "requested shutdown of the remaining local daemon"
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not (Test-Path -LiteralPath $EndpointPath -PathType Leaf)) {
            Write-Host "remaining local daemon exited"
            return
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $shutdownResponseReceived) {
        Write-Host "daemon closed the shutdown connection before replying; continuing with directory activation"
        return
    }
    throw "The remaining Codex Lite daemon did not exit within 15 seconds."
}

if (-not (Test-Path -LiteralPath (Join-Path $BuildDirectory "CodexLite.dll") -PathType Leaf)) {
    throw "Build output is incomplete: $BuildDirectory"
}

$desktopDirectory = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath("Desktop"))
# The independent helper must not retain the directory it is about to rename,
# even if its launcher inherited that native Windows working directory.
Write-Host "deployment helper native working directory: $([Environment]::CurrentDirectory)"
[Environment]::CurrentDirectory = $desktopDirectory
Set-Location -LiteralPath $desktopDirectory
$deploymentParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $DeploymentDirectory))
if (-not [string]::Equals($deploymentParent, $desktopDirectory, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to deploy outside the Windows desktop: $DeploymentDirectory"
}
$currentDirectory = Assert-ExactChildPath -RuntimeDirectory $deploymentParent -Candidate $DeploymentDirectory -ExpectedName "Codex Lite"
$nextDirectory = Assert-ExactChildPath -RuntimeDirectory $deploymentParent -Candidate (Join-Path $deploymentParent "Codex Lite-next") -ExpectedName "Codex Lite-next"
$previousDirectory = Assert-ExactChildPath -RuntimeDirectory $deploymentParent -Candidate (Join-Path $deploymentParent "Codex Lite-previous") -ExpectedName "Codex Lite-previous"

if (Test-Path -LiteralPath $nextDirectory) {
    Remove-Item -LiteralPath $nextDirectory -Recurse -Force
}
New-Item -ItemType Directory -Path $nextDirectory -Force | Out-Null
Copy-Item -Path (Join-Path $BuildDirectory "*") -Destination $nextDirectory -Recurse -Force
# Runtime configuration is administrator-owned, not an implicit build default.
# An explicitly packaged configuration replaces it; ordinary updates preserve it.
$installedCloudConfig = Join-Path $currentDirectory "cloud-sync.json"
$stagedCloudConfig = Join-Path $nextDirectory "cloud-sync.json"
if ((Test-Path -LiteralPath $installedCloudConfig -PathType Leaf) -and
    -not (Test-Path -LiteralPath $stagedCloudConfig -PathType Leaf)) {
    Copy-Item -LiteralPath $installedCloudConfig -Destination $stagedCloudConfig
    Write-Host "preserved installed cloud-sync.json"
}
Write-Host "staged $BuildDirectory -> $nextDirectory"

$protectedIds = @(
    $ProtectedProcessIds.Split(",", [System.StringSplitOptions]::RemoveEmptyEntries) |
        ForEach-Object { [int]$_ }
)
$wslChildren = @(Get-CodexLiteWslChildren)
Stop-UnprotectedCodexLite -ProtectedIds $protectedIds
Stop-RemainingLocalDaemon -EndpointPath $DaemonEndpointPath
Wait-CodexLiteWslChildrenExit -Children $wslChildren

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
