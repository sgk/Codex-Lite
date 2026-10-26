$ErrorActionPreference = "Stop"
$source = Join-Path $PSScriptRoot "../deploy-debug.ps1"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path -LiteralPath $source).ProviderPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw "Deployment script has syntax errors: $parseErrors" }
$definition = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Stop-RemainingLocalDaemon"
}, $true)
if ($null -eq $definition) { throw "Daemon shutdown function not found" }

# Execute the production function with deterministic file-system doubles.
# Never stop a real daemon or contact an endpoint in this test.
& {
    param($functionText)
    . ([scriptblock]::Create($functionText))
    $scenario = "missing"
    function Test-Path { param($LiteralPath, $PathType) return $scenario -ne "missing" }
    function Get-Content {
        param($LiteralPath, [switch]$Raw)
        if ($scenario -eq "removed") {
            throw [System.Management.Automation.ItemNotFoundException]::new("Endpoint removed after existence check")
        }
        if ($scenario -eq "removed-file") {
            throw [System.IO.FileNotFoundException]::new("Endpoint removed while opening file")
        }
        throw [System.IO.IOException]::new("Endpoint is unreadable")
    }
    Stop-RemainingLocalDaemon -EndpointPath "test-endpoint.json"
    $scenario = "removed"
    Stop-RemainingLocalDaemon -EndpointPath "test-endpoint.json"
    $scenario = "removed-file"
    Stop-RemainingLocalDaemon -EndpointPath "test-endpoint.json"
    $scenario = "unreadable"
    $failed = $false
    try { Stop-RemainingLocalDaemon -EndpointPath "test-endpoint.json" }
    catch [System.IO.IOException] { $failed = $true }
    if (-not $failed) { throw "Unexpected I/O errors must not be ignored" }
} $definition.Extent.Text
Write-Host "Deployment checks passed: syntax, missing endpoint, shutdown race, unexpected I/O failure"

$rotationFunctions = @("Assert-ExactChildPath", "Assert-DeploymentFolder", "Get-InstalledDeploymentDirectory", "Activate-StagedDeployment") | ForEach-Object {
    $name = $_
    $node = $ast.Find({ param($item)
        $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -eq $name
    }, $true)
    if ($null -eq $node) { throw "Missing rotation function: $name" }
    $node.Extent.Text
}
# Use the production rotation functions only, on fake application files in a
# fresh temporary directory. No real deployment, shutdown, or launch occurs.
& {
    param($functionText)
    . ([scriptblock]::Create($functionText))
    $testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("CodexLite-DeployTests-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $current = Join-Path $testRoot "current"
    $next = Join-Path $testRoot "next"
    $previous = Join-Path $testRoot "previous"
    $previous2 = Join-Path $testRoot "previous2"
    $other = Join-Path $testRoot "unrelated"
    function New-Version {
        param([string]$Version)
        New-Item -ItemType Directory -Path (Join-Path $next "daemon") -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $next "CodexLite.dll") -Value $Version
        Set-Content -LiteralPath (Join-Path $next "CodexLite.exe") -Value $Version
        Set-Content -LiteralPath (Join-Path $next "daemon/version.fixture") -Value $Version
    }
    function Assert-Version {
        param([string]$Path, [string]$Version)
        foreach ($relative in @("CodexLite.dll", "CodexLite.exe", "daemon/version.fixture")) {
            if ((Get-Content -LiteralPath (Join-Path $Path $relative) -Raw).Trim() -ne $Version) {
                throw "Unexpected version at $Path/$relative; expected $Version"
            }
        }
    }
    try {
        New-Item -ItemType Directory -Path $other | Out-Null
        Set-Content -LiteralPath (Join-Path $other "keep.fixture") -Value "untouched"
        New-Version "A"
        Activate-StagedDeployment $testRoot
        Assert-Version $current "A"
        if (Test-Path -LiteralPath $previous) { throw "First installation must not invent a previous version" }
        foreach ($version in @("B", "C", "D")) {
            New-Version $version
            Activate-StagedDeployment $testRoot
        }
        Assert-Version $current "D"
        Assert-Version $previous "C"
        Assert-Version $previous2 "B"
        if (Test-Path -LiteralPath $next) { throw "Staging directory must be activated" }
        if ((Get-ChildItem -LiteralPath $testRoot -Directory).Count -ne 4) { throw "Keep exactly three application folders plus the unrelated fixture" }

        # The documented three renames switch versions without an app, script,
        # shortcut change, copying files, or discarding either version.
        $swap = Join-Path $testRoot "swap"
        Move-Item -LiteralPath $current -Destination $swap
        Move-Item -LiteralPath $previous -Destination $current
        Move-Item -LiteralPath $swap -Destination $previous
        Assert-Version $current "C"
        Assert-Version $previous "D"
        New-Version "E"
        Activate-StagedDeployment $testRoot
        Assert-Version $current "E"
        Assert-Version $previous "C"
        Assert-Version $previous2 "D"

        # An incomplete next version cannot remove any existing generation.
        New-Version "broken"
        Remove-Item -LiteralPath (Join-Path $next "CodexLite.dll")
        $failed = $false
        try { Activate-StagedDeployment $testRoot } catch { $failed = $true }
        if (-not $failed) { throw "Incomplete staging must fail" }
        Assert-Version $current "E"
        Assert-Version $previous "C"
        Assert-Version $previous2 "D"
        Set-Content -LiteralPath (Join-Path $next "CodexLite.dll") -Value "broken"

        # A non-app directory in a backup slot must not be recursively deleted.
        Remove-Item -LiteralPath (Join-Path $previous2 "CodexLite.dll")
        $failed = $false
        try { Activate-StagedDeployment $testRoot } catch { $failed = $true }
        if (-not $failed) { throw "Unknown backup directory must fail validation" }
        Assert-Version $current "E"
        Assert-Version $previous "C"
        if (-not (Test-Path -LiteralPath (Join-Path $previous2 "daemon/version.fixture"))) { throw "Invalid backup must remain untouched" }
        if ((Get-Content -LiteralPath (Join-Path $other "keep.fixture") -Raw).Trim() -ne "untouched") { throw "Unrelated directory changed" }

        & {
            param($path)
            $linkType = ""
            function Get-Item {
                param($LiteralPath, [switch]$Force)
                return [pscustomobject]@{ PSIsContainer = $true; LinkType = $linkType; Attributes = [System.IO.FileAttributes]::ReparsePoint }
            }
            Assert-DeploymentFolder $path
            $linkType = "Junction"
            $failed = $false
            try { Assert-DeploymentFolder $path } catch { $failed = $true }
            if (-not $failed) { throw "A directory junction must not be rotated or deleted" }
        } $previous

        # Failure after archiving the running version leaves named, recoverable
        # backups; there is no automatic selection of another executable.
        Set-Content -LiteralPath (Join-Path $previous2 "CodexLite.dll") -Value "D"
        & {
            param($root, $stagedPath)
            function Move-Item {
                param($LiteralPath, $Destination)
                if ($LiteralPath -eq $stagedPath) { throw [System.IO.IOException]::new("Simulated activation failure") }
                Microsoft.PowerShell.Management\Move-Item -LiteralPath $LiteralPath -Destination $Destination
            }
            $failed = $false
            try { Activate-StagedDeployment $root } catch [System.IO.IOException] { $failed = $true }
            if (-not $failed) { throw "Activation failure must be reported" }
        } $testRoot $next
        if (Test-Path -LiteralPath $current) { throw "Activation failure must not silently restore or launch another version" }
        Assert-Version $previous "E"
        Assert-Version $previous2 "C"
        Assert-Version $next "broken"
        Move-Item -LiteralPath $previous -Destination $current
        Assert-Version $current "E"

        # The old flat layout migrates wholly inside its existing container.
        $flatRoot = Join-Path $testRoot "flat-installation"
        New-Item -ItemType Directory -Path $flatRoot | Out-Null
        Copy-Item -Path (Join-Path $current "*") -Destination $flatRoot -Recurse
        Copy-Item -LiteralPath $previous2 -Destination (Join-Path $flatRoot "previous") -Recurse
        Copy-Item -LiteralPath $next -Destination (Join-Path $flatRoot "next") -Recurse
        Set-Content -LiteralPath (Join-Path $flatRoot "recovery.txt") -Value "inside only"
        if ((Get-InstalledDeploymentDirectory $flatRoot) -ne $flatRoot) { throw "Flat configuration path must be explicit" }
        Activate-StagedDeployment $flatRoot
        Assert-Version (Join-Path $flatRoot "current") "broken"
        Assert-Version (Join-Path $flatRoot "previous") "E"
        Assert-Version (Join-Path $flatRoot "previous2") "C"
        if (Test-Path -LiteralPath (Join-Path $flatRoot "CodexLite.dll")) { throw "Flat runtime must no longer remain at container root" }
        if ((Get-Content -LiteralPath (Join-Path $flatRoot "recovery.txt") -Raw).Trim() -ne "inside only") { throw "Recovery guide must stay in the container" }
        if ((Get-InstalledDeploymentDirectory $flatRoot) -ne (Join-Path $flatRoot "current")) { throw "New configuration path must use current" }
        Set-Content -LiteralPath (Join-Path $flatRoot "CodexLite.dll") -Value "unfinished migration"
        $failed = $false
        try { Get-InstalledDeploymentDirectory $flatRoot | Out-Null } catch { $failed = $true }
        if (-not $failed) { throw "Mixed flat and current layout must not silently select a version" }
    }
    finally {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
} ($rotationFunctions -join "`n")
Write-Host "Deployment rotation checks passed: three generations, full folder preservation, manual switch, redeploy, incomplete stage, invalid backup, OneDrive vs junction, activation failure, manual recovery, unrelated files"
