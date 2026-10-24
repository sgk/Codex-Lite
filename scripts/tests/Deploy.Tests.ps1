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
        throw [System.IO.IOException]::new("Endpoint is unreadable")
    }
    Stop-RemainingLocalDaemon -EndpointPath "test-endpoint.json"
    $scenario = "removed"
    Stop-RemainingLocalDaemon -EndpointPath "test-endpoint.json"
    $scenario = "unreadable"
    $failed = $false
    try { Stop-RemainingLocalDaemon -EndpointPath "test-endpoint.json" }
    catch [System.IO.IOException] { $failed = $true }
    if (-not $failed) { throw "Unexpected I/O errors must not be ignored" }
} $definition.Extent.Text
Write-Host "Deployment checks passed: syntax, missing endpoint, shutdown race, unexpected I/O failure"
