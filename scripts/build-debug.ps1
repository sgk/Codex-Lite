param(
    [string]$Configuration = "Debug",
    [string]$Solution = "windows/CodexLite.sln",
    [string]$Dotnet = "C:\Program Files\dotnet\dotnet.exe",
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
        [string]$RepoRoot,
        [string]$DeploymentDirectory
    )

    $runtimeRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "runtime"))
    $deploymentPath = [System.IO.Path]::GetFullPath($DeploymentDirectory)
    $expectedPath = [System.IO.Path]::GetFullPath((Join-Path $runtimeRoot "CodexLite"))
    if (-not [string]::Equals($deploymentPath, $expectedPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace unexpected deployment directory: $deploymentPath"
    }
    return $deploymentPath
}

$repoRoot = Get-RepoRoot
Set-Location $repoRoot
$buildDirectory = Join-Path $repoRoot "windows/CodexLite/bin/$Configuration/net8.0-windows"
$deploymentDirectory = Assert-DeploymentPath -RepoRoot $repoRoot -DeploymentDirectory (Join-Path $repoRoot "runtime/CodexLite")

$ancestry = @(Get-ProcessAncestry -ProcessId $PID)
$protectedProcessIds = @($ancestry | ForEach-Object { [int]$_.ProcessId })
Write-Host "protected process chain:"
foreach ($proc in $ancestry) {
    Write-Host ("  {0} <- {1} {2}" -f $proc.ProcessId, $proc.ParentProcessId, $proc.Name)
}

if ($DryRun) {
    Write-Host "would build, then hand deployment to an independent Windows process"
    Write-Host "would deploy $buildDirectory -> $deploymentDirectory"
    exit 0
}

& $Dotnet build $Solution -c $Configuration
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$runtimeDirectory = Split-Path -Parent $deploymentDirectory
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$deployScript = Join-Path $repoRoot "scripts/deploy-debug.ps1"
$protectedIds = $protectedProcessIds -join ","
$deployLog = Join-Path $runtimeDirectory "deploy-debug.log"
$deployArguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $deployScript,
    "-BuildDirectory", $buildDirectory,
    "-DeploymentDirectory", $deploymentDirectory,
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
