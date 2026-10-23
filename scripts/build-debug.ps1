param(
    [string]$Configuration = "Debug",
    [string]$Solution = "windows/CodexLite.sln",
    [string]$Dotnet = "C:\Program Files\dotnet\dotnet.exe",
    [string]$CloudSyncConfigFile = "",
    [switch]$NoLaunch,
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

function Get-RepoRoot {
    $scriptDir = Split-Path -Parent $PSCommandPath
    return (Resolve-Path (Join-Path $scriptDir "..")).ProviderPath
}

function Assert-DeploymentPath {
    param(
        [string]$DeploymentDirectory
    )

    $desktopDirectory = [Environment]::GetFolderPath("Desktop")
    if ([string]::IsNullOrWhiteSpace($desktopDirectory)) {
        throw "The Windows desktop directory could not be resolved."
    }
    $deploymentPath = [System.IO.Path]::GetFullPath($DeploymentDirectory)
    $expectedPath = [System.IO.Path]::GetFullPath((Join-Path $desktopDirectory "Codex Lite"))
    if (-not [string]::Equals($deploymentPath, $expectedPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace unexpected deployment directory: $deploymentPath"
    }
    return $deploymentPath
}

function Convert-ToWslPath {
    param([string]$WindowsPath)

    $fullPath = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($fullPath -match '^\\\\wsl\.localhost\\[^\\]+\\(.+)$' -or
        $fullPath -match '^\\\\wsl\$\\[^\\]+\\(.+)$') {
        return "/" + $matches[1].Replace('\', '/')
    }
    if ($fullPath -match '^([A-Za-z]):\\(.*)$') {
        return "/mnt/" + $matches[1].ToLowerInvariant() + "/" + $matches[2].Replace('\', '/')
    }
    throw "Unsupported repository path for WSL: $fullPath"
}

function Convert-WslPathToUncPath {
    param(
        [string]$RepoRoot,
        [string]$WslPath
    )

    if ($RepoRoot -notmatch '^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\') {
        throw "The WSL distribution could not be resolved from the repository path: $RepoRoot"
    }
    $distroName = $matches[1]
    return "\\wsl.localhost\$distroName" + $WslPath.Replace('/', '\')
}

$repoRoot = Get-RepoRoot
Set-Location $repoRoot
$cloudSyncConfigPath = ""
if ($CloudSyncConfigFile) {
    $cloudSyncConfigPath = (Resolve-Path -LiteralPath $CloudSyncConfigFile).ProviderPath
}
$buildDirectory = Join-Path $repoRoot "windows/CodexLite/bin/$Configuration/net8.0-windows"
$desktopDirectory = [Environment]::GetFolderPath("Desktop")
$deploymentDirectory = Assert-DeploymentPath -DeploymentDirectory (Join-Path $desktopDirectory "Codex Lite")

$ancestry = @(Get-ProcessAncestry -ProcessId $PID)
$protectedProcessIds = @($ancestry | ForEach-Object { [int]$_.ProcessId })
Write-Host "protected process chain:"
foreach ($proc in $ancestry) {
    Write-Host ("  {0} <- {1} {2}" -f $proc.ProcessId, $proc.ParentProcessId, $proc.Name)
}
$wslRepoRoot = Convert-ToWslPath -WindowsPath $repoRoot

if ($DryRun) {
    Write-Host "would build Remote Agent without a built-in Firebase destination"
    Write-Host "include explicitly selected cloud configuration: $([bool]$cloudSyncConfigPath)"
    Write-Host "would build, then hand deployment to an independent Windows process"
    Write-Host "would deploy $buildDirectory -> $deploymentDirectory"
    exit 0
}

$wslHome = ((& wsl.exe -- /bin/sh -c 'printf "%s" "$HOME"') -join "").Trim()
if ([string]::IsNullOrWhiteSpace($wslHome)) {
    throw "The default WSL home directory could not be resolved."
}
$daemonEndpointPath = Convert-WslPathToUncPath -RepoRoot $repoRoot -WslPath "$wslHome/.local/share/codex-lite/daemon-endpoint.json"

& wsl.exe -- npm --prefix "$wslRepoRoot/remote" run build:agent
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $Dotnet build $Solution -c $Configuration "-p:CloudSyncConfigPath=$cloudSyncConfigPath"
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$logDirectory = Join-Path $repoRoot "runtime"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$deployScript = Join-Path $repoRoot "scripts/deploy-debug.ps1"
$protectedIds = $protectedProcessIds -join ","
$deployLog = Join-Path $logDirectory "deploy-debug.log"
$deployArguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $deployScript,
    "-BuildDirectory", $buildDirectory,
    "-DeploymentDirectory", $deploymentDirectory,
    "-DaemonEndpointPath", $daemonEndpointPath,
    "-ProtectedProcessIds", $protectedIds,
    "-LogPath", $deployLog
)
if ($NoLaunch) {
    $deployArguments += "-NoLaunch"
}
$quotedArguments = $deployArguments | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }
$commandLine = '"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" ' + ($quotedArguments -join " ")
$deployProcess = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine = $commandLine}
if ($deployProcess.ReturnValue -ne 0) {
    throw "Failed to start independent deployment process: Win32 error $($deployProcess.ReturnValue)"
}
Write-Host "deployment handed to independent Windows process PID $($deployProcess.ProcessId)"
Write-Host "deployment log: $deployLog"
