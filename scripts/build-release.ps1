param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [string]$Configuration,
    [Parameter(Mandatory = $true)]
    [string]$Runtime,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [Parameter(Mandatory = $true)]
    [string]$ZipPath,
    [string]$CloudSyncConfigFile = "",
    [string]$Dotnet = "C:\Program Files\dotnet\dotnet.exe"
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    return (Resolve-Path (Join-Path (Split-Path -Parent $PSCommandPath) "..")).ProviderPath
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

$repoRoot = Get-RepoRoot
Set-Location $repoRoot
$cloudSyncConfigPath = ""
if ($CloudSyncConfigFile) {
    $cloudSyncConfigPath = (Resolve-Path -LiteralPath $CloudSyncConfigFile).ProviderPath
}
$outputPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
$archivePath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $ZipPath))
$wslRepoRoot = Convert-ToWslPath -WindowsPath $repoRoot

if (Test-Path -LiteralPath $outputPath) {
    Remove-Item -LiteralPath $outputPath -Recurse -Force
}
if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
    Remove-Item -LiteralPath $archivePath -Force
}
New-Item -ItemType Directory -Path (Split-Path -Parent $outputPath) -Force | Out-Null

& wsl.exe -- npm --prefix "$wslRepoRoot/remote" run build:agent
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $Dotnet publish "windows/CodexLite/CodexLite.csproj" `
    -c $Configuration `
    -r $Runtime `
    --self-contained false `
    -p:PublishSingleFile=false `
    "-p:Version=$Version" `
    "-p:CloudSyncConfigPath=$cloudSyncConfigPath" `
    -o $outputPath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Compress-Archive -Path (Join-Path $outputPath "*") -DestinationPath $archivePath -Force
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    throw "Release archive was not created: $archivePath"
}
Write-Host "Included explicit cloud configuration: $([bool]$cloudSyncConfigPath)"
Write-Host "Release archive: $archivePath"
