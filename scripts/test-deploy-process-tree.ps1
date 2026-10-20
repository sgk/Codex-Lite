$ErrorActionPreference = "Stop"
$tokens = $null
$parseErrors = $null
$scriptPath = Join-Path $PSScriptRoot "deploy-debug.ps1"
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "Deployment script has parse errors." }
$functionAst = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Get-CodexLiteWslChildren"
}, $true)
if ($null -eq $functionAst) { throw "Deployment process discovery function was not found." }
. ([scriptblock]::Create($functionAst.Extent.Text))

$now = [datetime]::UtcNow
$script:snapshot = @(
    [pscustomobject]@{ ProcessId = 10; ParentProcessId = 1; Name = "CodexLite.exe"; CreationDate = $now },
    [pscustomobject]@{ ProcessId = 20; ParentProcessId = 10; Name = "wsl.exe"; CreationDate = $now.AddSeconds(1) },
    [pscustomobject]@{ ProcessId = 30; ParentProcessId = 20; Name = "wslhost.exe"; CreationDate = $now.AddSeconds(2) },
    # The old host's parent PID was reused by the new wsl.exe.
    [pscustomobject]@{ ProcessId = 40; ParentProcessId = 20; Name = "wslhost.exe"; CreationDate = $now.AddDays(-1) },
    [pscustomobject]@{ ProcessId = 50; ParentProcessId = 40; Name = "wsl.exe"; CreationDate = $now.AddSeconds(3) },
    [pscustomobject]@{ ProcessId = 60; ParentProcessId = 10; Name = "wslhost.exe"; CreationDate = $null }
)
function Get-CimInstance { param([string]$ClassName) return $script:snapshot }
$found = @(Get-CodexLiteWslChildren | Sort-Object ProcessId | ForEach-Object { $_.ProcessId })
if (($found -join ",") -ne "20,30") {
    throw "Unexpected descendants: $($found -join ','). PID reuse or missing creation time must not establish ownership."
}
Write-Host "PASS: real WSL descendants included; reused parent PID and unknown creation time excluded."
