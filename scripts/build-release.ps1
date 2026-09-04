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
    [string]$EnvFile = ".env",
    [string]$Dotnet = "C:\Program Files\dotnet\dotnet.exe"
)

$ErrorActionPreference = "Stop"

function Read-DotEnvFile {
    param([string]$Path)

    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $values
    }
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith("#")) {
            continue
        }
        if ($line.StartsWith("export ")) {
            $line = $line.Substring(7).TrimStart()
        }
        $separator = $line.IndexOf('=')
        if ($separator -le 0) {
            throw "Invalid .env line: expected NAME=VALUE."
        }
        $name = $line.Substring(0, $separator).Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            throw "Invalid .env variable name: $name"
        }
        $value = $line.Substring($separator + 1).Trim()
        if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
            $value = $value.Substring(1, $value.Length - 2)
            $value = $value.Replace('\n', "`n").Replace('\r', "`r").Replace('\"', '"').Replace('\\', '\')
        }
        elseif ($value.Length -ge 2 -and $value.StartsWith("'") -and $value.EndsWith("'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$name] = $value
    }
    return $values
}

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
$resolvedEnvFile = if ([System.IO.Path]::IsPathRooted($EnvFile)) { $EnvFile } else { Join-Path $repoRoot $EnvFile }
$envValues = Read-DotEnvFile -Path $resolvedEnvFile
$oauthClientSecret = [string]$envValues["GOOGLE_OAUTH_CLIENT_SECRET"]
$oauthClientId = [string]$envValues["GOOGLE_OAUTH_CLIENT_ID"]
$remoteSyncEnabled = -not [string]::IsNullOrWhiteSpace($oauthClientSecret)
if ($remoteSyncEnabled -and [string]::IsNullOrWhiteSpace($oauthClientId)) {
    throw "GOOGLE_OAUTH_CLIENT_ID is required when GOOGLE_OAUTH_CLIENT_SECRET is set."
}
$remoteSyncEnabledText = if ($remoteSyncEnabled) { "true" } else { "false" }
$generatedOAuthConfigPath = Join-Path $repoRoot "windows/CodexLite/obj/remote-oauth.release.generated.json"
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

if ($remoteSyncEnabled) {
    & wsl.exe -- npm --prefix "$wslRepoRoot/remote" run build:agent
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $generatedOAuthConfigPath) -Force | Out-Null
    $oauthConfig = @{ clientId = $oauthClientId; clientSecret = $oauthClientSecret } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($generatedOAuthConfigPath, $oauthConfig, (New-Object System.Text.UTF8Encoding($false)))
}

try {
    & $Dotnet publish "windows/CodexLite/CodexLite.csproj" `
        -c $Configuration `
        -r $Runtime `
        --self-contained false `
        -p:PublishSingleFile=false `
        "-p:Version=$Version" `
        "-p:RemoteSyncEnabled=$remoteSyncEnabledText" `
        "-p:RemoteOAuthConfigPath=$generatedOAuthConfigPath" `
        -o $outputPath
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    if (Test-Path -LiteralPath $generatedOAuthConfigPath -PathType Leaf) {
        Remove-Item -LiteralPath $generatedOAuthConfigPath -Force
    }
}

Compress-Archive -Path (Join-Path $outputPath "*") -DestinationPath $archivePath -Force
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    throw "Release archive was not created: $archivePath"
}
Write-Host "Remote sync build: $remoteSyncEnabledText"
Write-Host "Release archive: $archivePath"
