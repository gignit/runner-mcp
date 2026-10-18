# runner-mcp -- build / install on Windows (the Makefile's counterpart; no make needed).
#
#   .\build.ps1             Compile for this machine (mcp\dist + bin\runner-cli.exe)
#   .\build.ps1 -Stage      ...and stage dist\runner-mcp-<ver>-windows-<arch>.zip + SHA256SUMS
#                           (both windows-amd64 and windows-arm64)
#   .\build.ps1 -Install    Stage, then run install.ps1 against the local dist
#                           (tests the exact installer users run)
#   .\build.ps1 -Clean      Remove build artifacts
#
# `make release` (macOS/Linux) builds and publishes every platform, Windows
# included; this script is for building and testing on Windows itself.

param([switch]$Stage, [switch]$Install, [switch]$Clean)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Tool = "runner-mcp"
$Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
$BinDir = Join-Path $Root "bin"
$DistDir = Join-Path $Root "dist"
$WinPlatforms = @("windows-amd64", "windows-arm64")

function Step($m) { Write-Host "> $m" }
function Ok($m) { Write-Host "+ $m" -ForegroundColor Green }

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$Dir)
    Push-Location $Dir
    try {
        & $Exe @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
}

function Find-Go {
    $go = Get-Command go -ErrorAction SilentlyContinue
    if ($go) { return $go.Source }
    $p = Join-Path $env:ProgramFiles "Go\bin\go.exe"
    if (Test-Path $p) { return $p }
    throw "go is required to build runner-cli (winget install GoLang.Go)"
}

if ($Clean) {
    foreach ($d in @($BinDir, $DistDir, (Join-Path $Root "mcp\dist"))) { if (Test-Path $d) { Remove-Item $d -Recurse -Force } }
    Ok "Cleaned build artifacts"
    return
}

# --- Build (this machine) ----------------------------------------------------
# --omit=optional: the Claude Agent SDK's optional dependencies are ~100MB of
# platform-specific claude binaries; lib\agents\claude_runner.mjs uses the
# user's own `claude` instead (same as the Makefile).
Step "Building MCP server (TypeScript -> mcp\dist)"
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npm) { throw "npm is required (winget install OpenJS.NodeJS.LTS)" }
Invoke-Native $npm @("install", "--omit=optional", "--silent") (Join-Path $Root "mcp")
Invoke-Native $npm @("run", "build", "--silent") (Join-Path $Root "mcp")
Ok "MCP built at mcp\dist"

$go = Find-Go
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$prev = @{ GOOS = $env:GOOS; GOARCH = $env:GOARCH; CGO_ENABLED = $env:CGO_ENABLED }
try {
    $env:CGO_ENABLED = "0"
    foreach ($p in $WinPlatforms) {
        $env:GOOS, $env:GOARCH = $p.Split('-')
        Step "Building TUI for $p"
        Invoke-Native $go @("build", "-o", (Join-Path $BinDir "runner-cli-$p.exe"), ".") (Join-Path $Root "tui")
    }
} finally {
    foreach ($k in $prev.Keys) { Set-Item "Env:\$k" -Value $prev[$k] -ErrorAction SilentlyContinue; if (-not $prev[$k]) { Remove-Item "Env:\$k" -ErrorAction SilentlyContinue } }
}
$hostArch = if (($env:PROCESSOR_ARCHITEW6432, $env:PROCESSOR_ARCHITECTURE) -contains "ARM64") { "arm64" } else { "amd64" }
Copy-Item (Join-Path $BinDir "runner-cli-windows-$hostArch.exe") (Join-Path $BinDir "runner-cli.exe") -Force
Ok "Built $Tool v$Version (bin\runner-cli.exe)"

if (-not ($Stage -or $Install)) { return }

# --- Stage the payload zips (same tree as the Makefile's _stage) -------------
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
foreach ($p in $WinPlatforms) {
    $stageRoot = Join-Path $DistDir "stage-$p"
    $stage = Join-Path $stageRoot $Tool
    if (Test-Path $stageRoot) { Remove-Item $stageRoot -Recurse -Force }
    foreach ($d in @("core", "lib", "docs", "mcp", "bin")) { New-Item -ItemType Directory -Force -Path (Join-Path $stage $d) | Out-Null }
    Copy-Item (Join-Path $Root "core\*") (Join-Path $stage "core") -Recurse
    Copy-Item (Join-Path $Root "lib\*") (Join-Path $stage "lib") -Recurse
    Copy-Item (Join-Path $Root "docs\*") (Join-Path $stage "docs") -Recurse
    Copy-Item (Join-Path $Root "LICENSE") $stage
    Copy-Item (Join-Path $Root "mcp\dist") (Join-Path $stage "mcp\dist") -Recurse
    Copy-Item (Join-Path $Root "mcp\node_modules") (Join-Path $stage "mcp\node_modules") -Recurse
    Copy-Item (Join-Path $Root "mcp\package.json") (Join-Path $stage "mcp")
    Copy-Item (Join-Path $BinDir "runner-cli-$p.exe") (Join-Path $stage "bin\runner-cli.exe")
    Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    $zip = Join-Path $DistDir "$Tool-$Version-$p.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $stage -DestinationPath $zip
    Remove-Item $stageRoot -Recurse -Force
    Ok "Staged $zip"
}
$sums = Get-ChildItem $DistDir -Filter "*.zip" | ForEach-Object { "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower(), $_.Name }
[IO.File]::WriteAllText((Join-Path $DistDir "SHA256SUMS"), (($sums -join "`n") + "`n"))
Ok "Wrote dist\SHA256SUMS"

if ($Install) {
    Step "Running install.ps1 against local dist"
    & (Join-Path $Root "install.ps1") -Dist $DistDir -Version $Version
}
