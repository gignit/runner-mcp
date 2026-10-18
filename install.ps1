# runner-mcp installer for Windows 10/11 (Windows PowerShell 5.1 or PowerShell 7).
#
# Install or upgrade:
#
#     irm https://github.com/gignit/runner-mcp/releases/latest/download/install.ps1 | iex
#
# Uninstall (a dry run; add -Yes to remove):
#
#     & ([scriptblock]::Create((irm https://github.com/gignit/runner-mcp/releases/latest/download/install.ps1))) -Uninstall
#
# What it does (the Windows counterpart of install.sh):
#   1. Detects the architecture and downloads runner-mcp-<version>-windows-<arch>.zip
#      from GitHub Releases (SHA-256 checked against SHA256SUMS), or uses a local
#      artifact dir when RUNNER_MCP_DIST is set (how build.ps1 -Install tests it).
#   2. Installs the payload into %LOCALAPPDATA%\runner-mcp (XDG_DATA_HOME\runner-mcp
#      when that is set), replacing only the payload folders: the global run index
#      and any runs stored there are kept.
#   3. Records the verified Python 3 in python.path (python3 on Windows is often the
#      Microsoft Store stub) and puts runner-cli.exe + runnerlog.cmd in
#      %LOCALAPPDATA%\runner-mcp\bin, added to the user PATH.
#   4. Registers the MCP server with the agents it finds (Claude Code, Codex,
#      opencode, Grok, VS Code / VS Code Insiders / Cursor): add if missing, repair
#      if the registered server script no longer exists, otherwise leave it alone.
#      JSON configs are edited with node (a requirement anyway), so no jq.
#
# Environment overrides: RUNNER_MCP_VERSION, RUNNER_MCP_DIST, XDG_DATA_HOME,
# RUNNER_MCP_NO_REGISTER (skip agent registration).

param(
    [switch]$Uninstall,
    [switch]$Yes,
    [string]$Version = $(if ($env:RUNNER_MCP_VERSION) { $env:RUNNER_MCP_VERSION } else { "0.1.0" }),
    [string]$Dist = $env:RUNNER_MCP_DIST,
    [switch]$NoRegister
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar

$Repo = "gignit/runner-mcp"
$Tool = "runner-mcp"
$MinNodeMajor = 18

function Step($m) { Write-Host "> $m" }
function Ok($m)   { Write-Host "+ $m" -ForegroundColor Green }
function Info($m) { Write-Host "  $m" }
function Warn($m) { Write-Host "! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "x $m" -ForegroundColor Red; throw $m }

function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# --- Paths -------------------------------------------------------------------
$DataHome = if ($env:XDG_DATA_HOME) { $env:XDG_DATA_HOME } elseif ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
$InstallDir = Join-Path $DataHome $Tool
$BinDir = Join-Path $InstallDir "bin"
$McpEntry = Join-Path $InstallDir "mcp\dist\index.js"
$PayloadParts = @("core", "lib", "docs", "mcp", "bin", "LICENSE")

# --- Platform ----------------------------------------------------------------
function Get-Platform {
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    switch ($arch) {
        "AMD64" { return "windows-amd64" }
        "ARM64" { return "windows-arm64" }
        default { Die "unsupported architecture: $arch (supported: AMD64, ARM64)" }
    }
}

# --- Dependencies ------------------------------------------------------------
# A real Python 3: run each candidate and ask it for its own path. The Store
# stub (WindowsApps\python.exe without Python installed) prints nothing.
function Find-Python {
    $cands = @()
    if ($env:RUNNER_PYTHON) { $cands += , @($env:RUNNER_PYTHON) }
    $cands += , @("python3"); $cands += , @("python"); $cands += , @("py", "-3")
    foreach ($c in $cands) {
        if (-not (Have $c[0])) { continue }
        try {
            $rest = @($c | Select-Object -Skip 1)
            $out = & $c[0] @rest -c "import sys; print(sys.version_info[0], sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -match '^3 (.+)$') { return $Matches[1].Trim() }
        } catch { }
    }
    return $null
}

function Find-GitBash {
    $cands = @()
    foreach ($v in @($env:RUNNER_GIT_BASH, $env:CLAUDE_CODE_GIT_BASH_PATH)) { if ($v) { $cands += $v } }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $d = Split-Path $git.Source
        foreach ($root in @((Split-Path $d), (Split-Path (Split-Path $d)))) {
            if ($root) { $cands += (Join-Path $root "bin\bash.exe") }
        }
    }
    foreach ($b in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if ($b) { $cands += (Join-Path $b "Git\bin\bash.exe") }
    }
    foreach ($c in $cands) { if ($c -and (Test-Path $c -PathType Leaf)) { return $c } }
    return $null
}

function Test-Deps {
    Step "Checking dependencies"
    $missing = @()
    $script:NodeBin = $null
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) {
        $v = (& $node.Source --version) -replace '^v', ''
        $major = [int]($v.Split('.')[0])
        if ($major -lt $MinNodeMajor) {
            Warn "node v$v is too old (need >= $MinNodeMajor). Fix: winget install OpenJS.NodeJS.LTS"
            $missing += "node"
        } else {
            $script:NodeBin = $node.Source
            Ok "node v$v ($($node.Source))"
        }
    } else {
        Warn "node is required (the MCP server runs on Node). Fix: winget install OpenJS.NodeJS.LTS"
        $missing += "node"
    }
    $script:PythonBin = Find-Python
    if ($script:PythonBin) { Ok "python 3 ($($script:PythonBin))" }
    else {
        Warn "Python 3 is required (the runner core is Python). Fix: winget install Python.Python.3.12"
        $missing += "python"
    }
    $bash = Find-GitBash
    if ($bash) { Ok "Git Bash ($bash) -- runs commands" }
    else { Warn "Git Bash not found -- commands will run under PowerShell instead. Recommended: winget install Git.Git" }
    if ($missing.Count -gt 0) { Die "missing required dependencies: $($missing -join ', ') -- install them (see fixes above) and re-run the installer." }
}

# --- Fetch + verify ----------------------------------------------------------
function Get-Payload($platform, $work) {
    $zip = "$Tool-$Version-$platform.zip"
    $zipPath = Join-Path $work $zip
    $sums = Join-Path $work "SHA256SUMS"
    if ($Dist) {
        Step "Using local artifact dir: $Dist"
        $src = Join-Path $Dist $zip
        if (-not (Test-Path $src)) { Die "artifact not found: $src" }
        Copy-Item $src $zipPath
        if (Test-Path (Join-Path $Dist "SHA256SUMS")) { Copy-Item (Join-Path $Dist "SHA256SUMS") $sums }
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $base = if ($Version -eq "latest") { "https://github.com/$Repo/releases/latest/download" } else { "https://github.com/$Repo/releases/download/v$Version" }
        Step "Downloading $zip from GitHub Releases"
        Invoke-WebRequest -UseBasicParsing -Uri "$base/$zip" -OutFile $zipPath
        try { Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS" -OutFile $sums } catch { Warn "SHA256SUMS not available; skipping checksum verify" }
    }
    if (Test-Path $sums) {
        Step "Verifying checksum"
        $line = Get-Content $sums | Where-Object { $_ -match "\s\*?$([regex]::Escape($zip))$" } | Select-Object -First 1
        if ($line) {
            $expected = ($line -split '\s+')[0].ToLower()
            $actual = (Get-FileHash -Algorithm SHA256 $zipPath).Hash.ToLower()
            if ($expected -ne $actual) { Die "checksum mismatch for $zip (expected $expected, got $actual)" }
            Ok "Checksum verified"
        } else { Warn "no checksum entry for $zip; skipping" }
    } else { Warn "no SHA256SUMS; skipping verification" }
    return $zipPath
}

# Replace only the payload folders, so the global run index and runs stored in
# the install dir survive an upgrade. A running runner-cli.exe cannot be
# overwritten on Windows, but it can be renamed: move it aside first.
function Install-Payload($zipPath, $work) {
    Step "Extracting payload"
    $stage = Join-Path $work "stage"
    Expand-Archive -Path $zipPath -DestinationPath $stage -Force
    $root = Join-Path $stage $Tool
    if (-not (Test-Path $root)) { Die "unexpected archive layout (no $Tool\ folder)" }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Get-ChildItem $InstallDir -Recurse -Filter "*.old-*" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    $exe = Join-Path $BinDir "runner-cli.exe"
    if (Test-Path $exe) { Rename-Item $exe ("runner-cli.exe.old-" + [DateTime]::Now.Ticks) }
    foreach ($part in $PayloadParts) {
        $src = Join-Path $root $part
        $dst = Join-Path $InstallDir $part
        if (-not (Test-Path $src)) { continue }
        if (Test-Path $src -PathType Container) {
            if (Test-Path $dst) {
                Get-ChildItem $dst -Force | Where-Object { $_.Name -notlike "*.old-*" } | Remove-Item -Recurse -Force
            } else { New-Item -ItemType Directory -Force -Path $dst | Out-Null }
            Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
        } else {
            Copy-Item -Path $src -Destination $dst -Force
        }
    }
    Set-Content -Path (Join-Path $InstallDir "python.path") -Value $script:PythonBin -Encoding ASCII
    Ok "Installed to $InstallDir"
}

function Install-Clis {
    Step "Putting the CLIs on PATH ($BinDir)"
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $shim = "@echo off`r`n""%~dp0..\lib\runnerlog.cmd"" %*`r`nexit /b %ERRORLEVEL%`r`n"
    [IO.File]::WriteAllText((Join-Path $BinDir "runnerlog.cmd"), $shim)
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @(); if ($userPath) { $parts = $userPath.Split(';') | Where-Object { $_ } }
    if ($parts -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable("Path", (($parts + $BinDir) -join ';'), "User")
        Ok "Added $BinDir to the user PATH (open a new terminal to pick it up)"
    } else { Ok "$BinDir already on the user PATH" }
    if (-not (($env:Path.Split(';')) -contains $BinDir)) { $env:Path = "$env:Path;$BinDir" }
}

# --- JSON config edits (through node) ----------------------------------------
# Ops: get-script <file> <a.b.c> -> the server script the entry launches (or "")
#      set <file> <a.b.c> (value JSON in RUNNER_JSON_VAL) | del <file> <a.b.c>
$JsonTool = @'
const fs = require("fs");
const [op, file, keyPath] = process.argv.slice(2);
const keys = keyPath.split(".");
let txt = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
if (txt.charCodeAt(0) === 0xFEFF) txt = txt.slice(1);
let o;
try { o = txt.trim() ? JSON.parse(txt) : {}; }
catch (e) { console.error("not plain JSON (comments?): " + file); process.exit(3); }
const get = (v, ks) => { for (const k of ks) { if (v == null || typeof v !== "object") return undefined; v = v[k]; } return v; };
const write = () => { const t = file + ".tmp"; fs.writeFileSync(t, JSON.stringify(o, null, 2) + "\n"); fs.renameSync(t, file); };
if (op === "get-script") {
  const r = get(o, keys);
  let s = "";
  if (r && typeof r === "object") {
    if (Array.isArray(r.command)) s = r.command[r.command.length - 1] || "";
    else if (Array.isArray(r.args) && r.args.length) s = r.args[r.args.length - 1];
    else s = r.command || "";
  }
  process.stdout.write(r === undefined ? "<none>" : String(s));
} else if (op === "set") {
  let c = o;
  for (const k of keys.slice(0, -1)) { if (typeof c[k] !== "object" || c[k] === null) c[k] = {}; c = c[k]; }
  c[keys[keys.length - 1]] = JSON.parse(process.env.RUNNER_JSON_VAL);
  write();
} else if (op === "del") {
  const p = get(o, keys.slice(0, -1));
  if (p && typeof p === "object" && keys[keys.length - 1] in p) { delete p[keys[keys.length - 1]]; write(); }
}
'@

function Invoke-Json($op, $file, $key, $value) {
    $tool = Join-Path $env:TEMP "runner-mcp-json-$PID.js"
    [IO.File]::WriteAllText($tool, $JsonTool)
    try {
        if ($null -ne $value) { $env:RUNNER_JSON_VAL = $value }
        $out = & $script:NodeBin $tool $op $file $key 2>&1
        $code = $LASTEXITCODE
    } finally {
        Remove-Item Env:\RUNNER_JSON_VAL -ErrorAction SilentlyContinue
        Remove-Item $tool -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) { throw "json edit failed ($file): $out" }
    return ($out -join "")
}

function ConvertTo-JsonString($s) { '"' + ($s -replace '\\', '\\' -replace '"', '\"') + '"' }

# Is $p an absolute path that no longer exists? Relative/empty can't be judged.
function Test-ScriptMissing($p) {
    if (-not $p -or $p -eq "<none>") { return $false }
    if (-not [IO.Path]::IsPathRooted($p)) { return $false }
    return -not (Test-Path $p)
}

# Add-if-missing / repair-if-dead for one JSON config entry.
function Register-JsonEntry($label, $cfg, $key, $entryJson) {
    try {
        $script = Invoke-Json "get-script" $cfg $key $null
        if ($script -ne "<none>") {
            if (Test-ScriptMissing $script) {
                Invoke-Json "del" $cfg $key $null | Out-Null
                Warn "$label runner entry launched missing $script -- re-registering"
            } else {
                Ok "$label already has a runner entry -- left unchanged"
                return
            }
        }
        Invoke-Json "set" $cfg $key $entryJson | Out-Null
        Ok "Registered with $label ($cfg)"
    } catch {
        Warn "$label found but registration failed: $_"
    }
}

# Pull the first absolute *.js path out of an agent CLI's "show entry" output.
function Get-EntryScriptFromOutput($text) {
    $m = [regex]::Match(($text -join "`n"), '[A-Za-z]:[\\/][^\s",]+?\.js')
    if ($m.Success) { return $m.Value }
    return ""
}

function Invoke-Quiet {
    param([string]$Exe, [string[]]$Arguments)
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $out = & $Exe @Arguments 2>&1; return @{ Code = $LASTEXITCODE; Out = ($out | Out-String) } }
    catch { return @{ Code = 1; Out = "$_" } }
    finally { $ErrorActionPreference = $prev }
}

$NodeJson = { ConvertTo-JsonString $script:NodeBin }
$EntryJson = { ConvertTo-JsonString $McpEntry }

function Register-Claude {
    $cli = Get-Command claude -ErrorAction SilentlyContinue
    $cfg = Join-Path $HOME ".claude.json"
    $entry = "{""type"":""stdio"",""command"":$(& $NodeJson),""args"":[$(& $EntryJson)],""env"":{}}"
    if ($cli) {
        $got = Invoke-Quiet $cli.Source @("mcp", "get", "runner")
        if ($got.Code -eq 0) {
            $s = Get-EntryScriptFromOutput $got.Out
            if (Test-ScriptMissing $s) {
                Invoke-Quiet $cli.Source @("mcp", "remove", "runner", "-s", "user") | Out-Null
                Warn "Claude Code runner entry launched missing $s -- re-registering"
            } else { Ok "Claude Code already has a runner entry -- left unchanged"; return }
        }
        $r = Invoke-Quiet $cli.Source @("mcp", "add", "runner", "-s", "user", "--", $script:NodeBin, $McpEntry)
        if ($r.Code -eq 0) { Ok "Registered with Claude Code at user scope (claude mcp add -s user)" }
        else { Warn "claude found but registration failed; run: claude mcp add runner -s user -- ""$($script:NodeBin)"" ""$McpEntry""" }
    } elseif (Test-Path $cfg) {
        # Claude Code installed without the CLI on PATH (e.g. the desktop app):
        # the user-scope entry lives in ~/.claude.json .mcpServers.
        Register-JsonEntry "Claude Code (user scope)" $cfg "mcpServers.runner" $entry
    }
}

function Register-Codex {
    $cli = Get-Command codex -ErrorAction SilentlyContinue
    if (-not $cli) { return }
    $got = Invoke-Quiet $cli.Source @("mcp", "get", "runner")
    if ($got.Code -eq 0) {
        $s = Get-EntryScriptFromOutput $got.Out
        if (Test-ScriptMissing $s) {
            Invoke-Quiet $cli.Source @("mcp", "remove", "runner") | Out-Null
            Warn "Codex runner entry launched missing $s -- re-registering"
        } else { Ok "Codex already has a runner entry -- left unchanged"; return }
    }
    $r = Invoke-Quiet $cli.Source @("mcp", "add", "runner", "--", $script:NodeBin, $McpEntry)
    if ($r.Code -eq 0) { Ok "Registered with Codex (codex mcp add)" }
    else { Warn "codex found but registration failed; run: codex mcp add runner -- ""$($script:NodeBin)"" ""$McpEntry""" }
}

function Register-Grok {
    $cli = Get-Command grok -ErrorAction SilentlyContinue
    if (-not $cli) { return }
    $list = Invoke-Quiet $cli.Source @("mcp", "list")
    $line = ($list.Out -split "`n") | Where-Object { $_ -match '(^|\s)runner:(\s|$)' } | Select-Object -First 1
    if ($line) {
        $s = Get-EntryScriptFromOutput $line
        if (Test-ScriptMissing $s) {
            Invoke-Quiet $cli.Source @("mcp", "remove", "runner") | Out-Null
            Warn "Grok runner entry launched missing $s -- re-registering"
        } else { Ok "Grok already has a runner entry -- left unchanged"; return }
    }
    $r = Invoke-Quiet $cli.Source @("mcp", "add", "runner", "--command", $script:NodeBin, "--args", $McpEntry)
    if ($r.Code -eq 0) { Ok "Registered with Grok (grok mcp add)" }
    else { Warn "grok found but registration failed; run: grok mcp add runner --command ""$($script:NodeBin)"" --args ""$McpEntry""" }
}

function Get-OpencodeConfig {
    $base = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME ".config" }
    return (Join-Path $base "opencode\opencode.json")
}

function Register-Opencode {
    $cfg = Get-OpencodeConfig
    if (-not (Test-Path (Split-Path $cfg))) { Info "opencode not detected; skipping"; return }
    if (-not (Test-Path $cfg)) { [IO.File]::WriteAllText($cfg, '{"$schema":"https://opencode.ai/config.json"}') }
    $entry = "{""type"":""local"",""command"":[$(& $NodeJson),$(& $EntryJson)],""enabled"":true,""timeout"":600000}"
    Register-JsonEntry "opencode" $cfg "mcp.runner" $entry
}

$VsVariants = @(@("VS Code", "Code"), @("VS Code Insiders", "Code - Insiders"), @("Cursor", "Cursor"))

function Register-VSCode {
    foreach ($v in $VsVariants) {
        $base = Join-Path $env:APPDATA "$($v[1])\User"
        if (-not (Test-Path $base)) { continue }
        $cfg = Join-Path $base "mcp.json"
        if (-not (Test-Path $cfg)) { [IO.File]::WriteAllText($cfg, '{"servers":{}}') }
        $entry = "{""type"":""stdio"",""command"":$(& $NodeJson),""args"":[$(& $EntryJson)]}"
        Register-JsonEntry $v[0] $cfg "servers.runner" $entry
    }
}

function Register-Agents {
    if ($NoRegister -or $env:RUNNER_MCP_NO_REGISTER) { Info "Skipping agent registration (RUNNER_MCP_NO_REGISTER set)"; return }
    Step "Registering MCP server with installed agents"
    Info "Server launch command: $($script:NodeBin) $McpEntry"
    Register-Opencode
    Register-Claude
    Register-Codex
    Register-Grok
    Register-VSCode
}

# --- Uninstall ---------------------------------------------------------------
# Removes exactly what was installed: the install dir (payload, global index,
# global-fallback runs), the bin dir on the user PATH, and the runner MCP
# registration from every agent. Never touches per-project .runner\ run data.
# Dry run unless -Yes.
function Invoke-Uninstall {
    Write-Host ""
    Step "Uninstalling $Tool"
    if (-not $Yes) { Warn "DRY RUN -- nothing will be removed. Re-run with -Yes to proceed." }
    Write-Host ""
    $node = Get-Command node -ErrorAction SilentlyContinue
    $script:NodeBin = if ($node) { $node.Source } else { $null }

    if (Test-Path $InstallDir) {
        if ($Yes) {
            try { Remove-Item $InstallDir -Recurse -Force; Ok "removed $InstallDir" }
            catch { Warn "could not remove all of $InstallDir (a runner-cli or run still open?): $_" }
        } else { Info "would remove $InstallDir (payload + global index + global-fallback runs)" }
    } else { Info "install dir not found: $InstallDir" }

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -and ($userPath.Split(';') -contains $BinDir)) {
        if ($Yes) {
            [Environment]::SetEnvironmentVariable("Path", (($userPath.Split(';') | Where-Object { $_ -and $_ -ne $BinDir }) -join ';'), "User")
            Ok "removed $BinDir from the user PATH"
        } else { Info "would remove $BinDir from the user PATH" }
    }

    $jsonTargets = @(@("opencode", (Get-OpencodeConfig), "mcp.runner"))
    $cfgClaude = Join-Path $HOME ".claude.json"
    if (-not (Have claude)) { $jsonTargets += , @("Claude Code (user scope)", $cfgClaude, "mcpServers.runner") }
    foreach ($v in $VsVariants) { $jsonTargets += , @($v[0], (Join-Path $env:APPDATA "$($v[1])\User\mcp.json"), "servers.runner") }
    foreach ($t in $jsonTargets) {
        if (-not (Test-Path $t[1])) { continue }
        if (-not $script:NodeBin) { Warn "$($t[0]) config present but node missing; remove $($t[2]) from $($t[1]) by hand"; continue }
        try { $s = Invoke-Json "get-script" $t[1] $t[2] $null } catch { Warn "$($t[0]): $_"; continue }
        if ($s -eq "<none>") { continue }
        if ($Yes) { Invoke-Json "del" $t[1] $t[2] $null | Out-Null; Ok "$($t[0]): removed $($t[2])" }
        else { Info "would remove $($t[2]) from $($t[1])" }
    }
    $cliTargets = @(
        @("Claude Code (user scope)", "claude", @("mcp", "remove", "runner", "-s", "user")),
        @("Codex", "codex", @("mcp", "remove", "runner")),
        @("Grok", "grok", @("mcp", "remove", "runner")))
    foreach ($t in $cliTargets) {
        $cli = Get-Command $t[1] -ErrorAction SilentlyContinue
        if (-not $cli) { continue }
        if ($Yes) {
            $r = Invoke-Quiet $cli.Source $t[2]
            if ($r.Code -eq 0) { Ok "$($t[0]): removed runner registration" } else { Info "$($t[0]): no runner registration to remove (or already gone)" }
        } else { Info "would run: $($t[1]) $($t[2] -join ' ')" }
    }
    Write-Host ""
    if ($Yes) { Ok "$Tool uninstalled."; Info "Per-project .runner\ run data was left untouched." }
    else { Warn "Dry run complete. Re-run with -Yes to actually uninstall." }
    Write-Host ""
}

# --- Main --------------------------------------------------------------------
function Invoke-Install {
    Write-Host ""
    Step "Installing $Tool v$Version"
    $platform = Get-Platform
    Info "Platform: $platform"
    Test-Deps
    $work = Join-Path ([IO.Path]::GetTempPath()) ("runner-mcp-install-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    try {
        $zip = Get-Payload $platform $work
        Install-Payload $zip $work
    } finally {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    Install-Clis
    Register-Agents
    Write-Host ""
    Ok "$Tool v$Version installed to $InstallDir"
    Info "CLIs: $BinDir\runner-cli.exe, $BinDir\runnerlog.cmd"
    Info "Restart your agent (Claude Code / Codex / opencode / Grok / VS Code) to load the MCP server."
    Write-Host ""
}

if ($Uninstall) { Invoke-Uninstall } else { Invoke-Install }
