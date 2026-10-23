# Explicit, one-time migration. Normal builds never read these legacy settings.
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).ProviderPath
$oauthPath = Join-Path $repoRoot ".env"
$firebasePath = Join-Path $repoRoot "remote/web/public/firebase-config.json"
$runtime = Join-Path ([Environment]::GetFolderPath("Desktop")) "Codex Lite"
$destination = Join-Path $runtime "cloud-sync.json"

if (-not (Test-Path -LiteralPath (Join-Path $runtime "CodexLite.exe") -PathType Leaf)) {
    throw "The desktop Codex Lite runtime directory was not found."
}
if (Test-Path -LiteralPath $destination) {
    throw "cloud-sync.json already exists; refusing to overwrite administrator settings."
}

$oauth = @{}
foreach ($rawLine in [System.IO.File]::ReadAllLines($oauthPath)) {
    $line = $rawLine.Trim()
    if ($line.StartsWith("export ")) { $line = $line.Substring(7).TrimStart() }
    $separator = $line.IndexOf('=')
    if ($separator -lt 1) { continue }
    $name = $line.Substring(0, $separator).Trim()
    if ($name -notin @("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")) { continue }
    $value = $line.Substring($separator + 1).Trim()
    if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
        $value = $value.Substring(1, $value.Length - 2)
        $value = $value.Replace('\n', "`n").Replace('\r', "`r").Replace('\"', '"').Replace('\\', '\')
    }
    elseif ($value.Length -ge 2 -and $value.StartsWith("'") -and $value.EndsWith("'")) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    if ($oauth.ContainsKey($name)) { throw "Duplicate OAuth setting in the legacy configuration." }
    $oauth[$name] = $value
}
foreach ($name in @("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")) {
    if ([string]::IsNullOrWhiteSpace($oauth[$name])) { throw "A required legacy OAuth setting is missing." }
}
try {
    $firebase = [System.IO.File]::ReadAllText($firebasePath) | ConvertFrom-Json
}
catch { throw "The legacy Firebase configuration could not be read; no file was created." }
$connection = @{}
foreach ($name in @("projectId", "apiKey", "appId")) {
    if ([string]::IsNullOrWhiteSpace($firebase.$name)) { throw "A required Firebase setting is missing." }
    $connection[$name] = [string]$firebase.$name
}
if (-not [string]::IsNullOrWhiteSpace($firebase.authDomain)) { $connection["authDomain"] = [string]$firebase.authDomain }
$config = @{
    enabled = $true
    firebase = $connection
    oauth = @{ clientId = $oauth["GOOGLE_OAUTH_CLIENT_ID"]; clientSecret = $oauth["GOOGLE_OAUTH_CLIENT_SECRET"] }
}
$temporary = Join-Path $runtime ("cloud-sync-migration-" + [Guid]::NewGuid().ToString("N") + ".tmp")
try {
    $json = $config | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::Move($temporary, $destination)
}
finally {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -Force }
}
Write-Output "Created desktop Codex Lite/cloud-sync.json from the existing configuration. No credential values were printed."
