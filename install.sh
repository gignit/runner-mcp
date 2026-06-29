#!/usr/bin/env sh
# runner-mcp installer.
#
# The ONLY supported install method. Run it directly:
#
#     curl -fsSL https://github.com/gignit/runner-mcp/releases/latest/download/install.sh | sh
#
# or pin a version:
#
#     curl -fsSL https://github.com/gignit/runner-mcp/releases/download/v0.1.0/install.sh | sh
#
# What it does:
#   1. Detects OS + arch (uname) and selects the matching release tarball.
#   2. Downloads runner-mcp-<version>-<os>-<arch>.tar.gz from GitHub Releases
#      (verifying its SHA-256 against SHA256SUMS), OR uses a local artifact
#      dir when RUNNER_MCP_DIST is set (this is how `make install` dogfoods
#      the installer without going through GitHub).
#   3. Installs the payload into the XDG data dir
#      ($XDG_DATA_HOME/runner-mcp, default ~/.local/share/runner-mcp).
#   4. Symlinks the CLIs (runner-cli, runnerlog) into ~/.local/bin.
#   5. Registers the MCP server with whatever supported agents are present
#      (opencode, Claude Code, Codex, Grok, VS Code / Cursor).
#
# Environment overrides:
#   RUNNER_MCP_VERSION   version/tag to install (default: baked-in version).
#   RUNNER_MCP_DIST      local dir containing the built tarball + SHA256SUMS
#                        (skips the GitHub download; used by `make install`).
#   XDG_DATA_HOME        install location root (default ~/.local/share).
#   RUNNER_MCP_NO_REGISTER  if set, skip agent registration.
#
# POSIX sh only -- no bashisms -- so it runs the same on macOS and Linux.

set -eu

# --- Version (stamped by `make release`; default for from-checkout runs) -----
RUNNER_MCP_VERSION="${RUNNER_MCP_VERSION:-0.1.0}"

REPO="gignit/runner-mcp"
TOOL="runner-mcp"

# --- Pretty output -----------------------------------------------------------
info() { printf '  %s\n' "$*"; }
step() { printf '> %s\n' "$*"; }
ok()   { printf '+ %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }
die()  { printf 'x %s\n' "$*" >&2; exit 1; }

# --- Detect OS / arch --------------------------------------------------------
detect_platform() {
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os" in
    Linux)  os="linux" ;;
    Darwin) os="darwin" ;;
    *) die "unsupported OS: $os (runner-mcp supports Linux and macOS)" ;;
  esac
  case "$arch" in
    x86_64|amd64)  arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) die "unsupported architecture: $arch (supported: amd64, arm64)" ;;
  esac
  PLATFORM="${os}-${arch}"
}

# --- Dependency checks -------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1; }

# Platform-aware "how to install <pkg>" suggestion.
install_hint() {
  pkg="$1"
  case "$(uname -s)" in
    Darwin) printf 'brew install %s' "$pkg" ;;
    Linux)
      if   need apt-get; then printf 'sudo apt-get install -y %s' "$pkg"
      elif need dnf;     then printf 'sudo dnf install -y %s' "$pkg"
      elif need pacman;  then printf 'sudo pacman -S %s' "$pkg"
      elif need apk;     then printf 'sudo apk add %s' "$pkg"
      elif need zypper;  then printf 'sudo zypper install -y %s' "$pkg"
      else printf 'install %s with your package manager' "$pkg"
      fi ;;
    *) printf 'install %s' "$pkg" ;;
  esac
}

# node version helper: returns major version integer, or 0 if absent/unknown.
node_major() {
  need node || { echo 0; return; }
  v="$(node --version 2>/dev/null | sed 's/^v//;s/\..*//')"
  case "$v" in (*[!0-9]*|"") echo 0 ;; (*) echo "$v" ;; esac
}

MIN_NODE_MAJOR=18

# Preflight: verify everything the installer AND the runtime need, report a
# full per-dependency status with a concrete fix for each problem, and stop
# before touching the system if anything required is missing.
check_deps() {
  step "Checking dependencies"
  hard_missing=""   # required -- block install
  soft_missing=""   # optional -- warn only (degraded features)

  # --- required at runtime ---
  if need node; then
    nm="$(node_major)"
    if [ "$nm" -lt "$MIN_NODE_MAJOR" ]; then
      warn "node $(node --version 2>/dev/null) is too old (need >= $MIN_NODE_MAJOR). Fix: $(install_hint node)"
      hard_missing="$hard_missing node"
    else
      ok "node $(node --version 2>/dev/null)"
    fi
  else
    warn "node is required (the MCP server runs on Node). Fix: $(install_hint node)"
    hard_missing="$hard_missing node"
  fi

  if need python3; then
    ok "python3 $(python3 --version 2>/dev/null | awk '{print $2}')"
  else
    warn "python3 is required (the runner core is Python). Fix: $(install_hint python3)"
    hard_missing="$hard_missing python3"
  fi

  # --- required by the installer itself (download + extract + verify) ---
  if need curl || need wget; then ok "downloader (curl/wget)"
  else
    warn "curl or wget is required to download release artifacts. Fix: $(install_hint curl)"
    hard_missing="$hard_missing curl"
  fi
  if need tar; then ok "tar"
  else warn "tar is required to extract the release. Fix: $(install_hint tar)"; hard_missing="$hard_missing tar"; fi
  if need sha256sum || need shasum; then ok "sha256 (checksum verify)"
  else warn "no sha256sum/shasum -- checksum verification will be skipped (not fatal)."; fi

  # --- optional: agent auto-registration ---
  if need jq; then ok "jq (opencode auto-registration)"
  else
    warn "jq not found -- opencode auto-registration will be skipped. Fix: $(install_hint jq)"
    soft_missing="$soft_missing jq"
  fi

  if [ -n "$hard_missing" ]; then
    printf '\n'
    die "missing required dependencies:$hard_missing -- install them (see fixes above) and re-run the installer."
  fi
  [ -z "$soft_missing" ] || warn "optional tools missing (degraded, not fatal):$soft_missing"
}

# --- Paths (XDG) -------------------------------------------------------------
data_home() {
  if [ -n "${XDG_DATA_HOME:-}" ]; then printf '%s' "$XDG_DATA_HOME"
  else printf '%s' "$HOME/.local/share"; fi
}

INSTALL_DIR="$(data_home)/$TOOL"
BIN_DIR="$HOME/.local/bin"

# --- Fetch + verify the payload ---------------------------------------------
fetch_payload() {
  tarball="${TOOL}-${RUNNER_MCP_VERSION}-${PLATFORM}.tar.gz"
  WORKDIR="$(mktemp -d)"
  trap 'rm -rf "$WORKDIR"' EXIT

  if [ -n "${RUNNER_MCP_DIST:-}" ]; then
    # Local artifact dir (make install path).
    step "Using local artifact dir: $RUNNER_MCP_DIST"
    [ -f "$RUNNER_MCP_DIST/$tarball" ] || die "artifact not found: $RUNNER_MCP_DIST/$tarball"
    cp "$RUNNER_MCP_DIST/$tarball" "$WORKDIR/$tarball"
    [ -f "$RUNNER_MCP_DIST/SHA256SUMS" ] && cp "$RUNNER_MCP_DIST/SHA256SUMS" "$WORKDIR/SHA256SUMS" || true
  else
    # GitHub Releases. Build a direct download URL -- a pinned version uses
    # the tag path; "latest" uses the latest-release redirect path.
    if [ "$RUNNER_MCP_VERSION" = "latest" ]; then
      base="https://github.com/$REPO/releases/latest/download"
    else
      base="https://github.com/$REPO/releases/download/v${RUNNER_MCP_VERSION}"
    fi
    step "Downloading $tarball from GitHub Releases"
    download "$base/$tarball"      "$WORKDIR/$tarball"      || die "failed to download $tarball"
    download "$base/SHA256SUMS"    "$WORKDIR/SHA256SUMS"    || warn "SHA256SUMS not available; skipping checksum verify"
  fi

  verify_checksum "$WORKDIR" "$tarball"

  step "Extracting payload"
  if need rsync; then
    # Idempotent in-place update: extract to a staging dir, then rsync into
    # place. Existing files (including the mcp/dist/index.js a running agent's
    # MCP server is using) are overwritten in place and stale files pruned --
    # the install is never momentarily missing, so reinstalling/upgrading does
    # not break agents whose server is live.
    staging="$WORKDIR/payload"
    mkdir -p "$staging"
    tar -xzf "$WORKDIR/$tarball" -C "$staging" --strip-components=1
    mkdir -p "$INSTALL_DIR"
    rsync -a --delete "$staging"/ "$INSTALL_DIR"/
  else
    # No rsync: original behavior -- wipe and re-extract for a clean install.
    rm -rf "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    tar -xzf "$WORKDIR/$tarball" -C "$INSTALL_DIR" --strip-components=1
  fi
}

download() {
  url="$1"; out="$2"
  if need curl; then
    curl -fsSL "$url" -o "$out"
  elif need wget; then
    wget -qO "$out" "$url"
  else
    die "need curl or wget to download release artifacts"
  fi
}

verify_checksum() {
  dir="$1"; file="$2"
  [ -f "$dir/SHA256SUMS" ] || { warn "no SHA256SUMS; skipping verification"; return 0; }
  step "Verifying checksum"
  expected="$(grep " $file\$" "$dir/SHA256SUMS" 2>/dev/null | awk '{print $1}' | head -n1)"
  [ -n "$expected" ] || { warn "no checksum entry for $file; skipping"; return 0; }
  if need sha256sum;   then actual="$(sha256sum "$dir/$file" | awk '{print $1}')"
  elif need shasum;    then actual="$(shasum -a 256 "$dir/$file" | awk '{print $1}')"
  else warn "no sha256 tool; skipping verification"; return 0; fi
  [ "$expected" = "$actual" ] || die "checksum mismatch for $file (expected $expected, got $actual)"
  ok "Checksum verified"
}

# --- Symlink CLIs onto PATH --------------------------------------------------
# `runner-cli` is the companion TUI for watching/managing runs; `runnerlog`
# is the script-instrumentation helper. (The MCP server itself is launched by
# agents directly from mcp/dist/index.js and is not a PATH command.)
link_clis() {
  step "Linking CLIs into $BIN_DIR"
  mkdir -p "$BIN_DIR"
  for spec in "runner-cli:bin/runner-cli" "runnerlog:lib/runnerlog"; do
    link="${spec%%:*}"
    target="$INSTALL_DIR/${spec#*:}"
    if [ -e "$target" ]; then
      chmod +x "$target" 2>/dev/null || true
      rm -f "$BIN_DIR/$link"
      ln -s "$target" "$BIN_DIR/$link"
      ok "Linked $BIN_DIR/$link"
    fi
  done
  case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;
    *) warn "$BIN_DIR is not on your PATH; add it to your shell profile:"
       warn "    export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
}

# --- Agent registration ------------------------------------------------------
MCP_ENTRY="$INSTALL_DIR/mcp/dist/index.js"

# Resolve the ABSOLUTE path to node and register agents to launch the server
# with it (not a bare `node`). Bare `node` breaks when an agent is launched
# from an environment that doesn't have node on PATH -- common on macOS where
# Homebrew's /opt/homebrew/bin is only added by interactive shell profiles, so
# a GUI-launched agent may not see it. The absolute path is bulletproof.
NODE_BIN="$(command -v node 2>/dev/null || echo node)"

register_agents() {
  [ -z "${RUNNER_MCP_NO_REGISTER:-}" ] || { info "Skipping agent registration (RUNNER_MCP_NO_REGISTER set)"; return 0; }
  step "Registering MCP server with installed agents"
  info "Server launch command: $NODE_BIN $MCP_ENTRY"
  register_opencode
  register_claude
  register_codex
  register_grok
  register_vscode
}

# Registration policy: ADD ONLY IF MISSING. If a `runner` entry already exists
# in an agent's config, we treat it as USER-OWNED and never modify it -- the
# user may have manually tuned options (timeout, env, custom command). The
# installer only creates the entry when none exists; updating/removing an
# existing one is left to the user. New entries launch the server with the
# absolute node path ($NODE_BIN) so GUI-launched agents find node reliably.

# opencode: JSON config at ~/.config/opencode/opencode.json, key .mcp.runner
register_opencode() {
  cfg="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
  need jq || { [ -f "$cfg" ] && warn "opencode found but jq missing; skipping (install jq to auto-register)"; return 0; }
  [ -d "$(dirname "$cfg")" ] || { info "opencode not detected; skipping"; return 0; }
  [ -f "$cfg" ] || printf '{"$schema":"https://opencode.ai/config.json"}' > "$cfg"
  if jq -e '.mcp.runner != null' "$cfg" >/dev/null 2>&1; then
    ok "opencode already has a runner entry -- left unchanged"
    return 0
  fi
  entry="$(jq -n --arg n "$NODE_BIN" --arg e "$MCP_ENTRY" \
        '{type:"local", command:[$n,$e], enabled:true, timeout:600000}')"
  tmp="$cfg.tmp.$$"
  jq --argjson r "$entry" '.mcp.runner = $r' "$cfg" > "$tmp" && mv "$tmp" "$cfg"
  ok "Registered with opencode ($cfg)"
}

# Claude Code: `claude mcp get` exits 0 if a runner entry exists.
register_claude() {
  need claude || return 0
  if claude mcp get runner >/dev/null 2>&1; then
    ok "Claude Code already has a runner entry -- left unchanged"
    return 0
  fi
  claude mcp add runner -- "$NODE_BIN" "$MCP_ENTRY" >/dev/null 2>&1 \
    && ok "Registered with Claude Code (claude mcp add)" \
    || warn "claude found but registration failed; run: claude mcp add runner -- \"$NODE_BIN\" \"$MCP_ENTRY\""
}

# Codex: `codex mcp get` exits 0 if a runner entry exists.
register_codex() {
  need codex || return 0
  if codex mcp get runner >/dev/null 2>&1; then
    ok "Codex already has a runner entry -- left unchanged"
    return 0
  fi
  codex mcp add runner -- "$NODE_BIN" "$MCP_ENTRY" >/dev/null 2>&1 \
    && ok "Registered with Codex (codex mcp add)" \
    || warn "codex found but registration failed; run: codex mcp add runner -- \"$NODE_BIN\" \"$MCP_ENTRY\""
}

# Grok: check `grok mcp list` for an existing runner entry.
register_grok() {
  need grok || return 0
  if grok mcp list 2>/dev/null | grep -Eq '(^|[[:space:]])runner([[:space:]]|$)'; then
    ok "Grok already has a runner entry -- left unchanged"
    return 0
  fi
  grok mcp add runner --command "$NODE_BIN" --args "$MCP_ENTRY" >/dev/null 2>&1 \
    && ok "Registered with Grok (grok mcp add)" \
    || warn "grok found but registration failed; run: grok mcp add runner --command \"$NODE_BIN\" --args \"$MCP_ENTRY\""
}

# VS Code family (VS Code, VS Code Insiders, Cursor). Registers runner with
# each variant that is present. Each has a user-profile mcp.json (key:
# "servers") and a CLI `--add-mcp`. We add ONLY IF MISSING (never touch an
# existing entry). We register via the mcp.json file directly (jq) rather than
# the CLI, because the `code`/`cursor` CLI is frequently not on PATH (e.g. on
# macOS until the user runs "Install 'code' command in PATH"), whereas the
# config dir is always present when the app is installed.
register_vscode() {
  # variant "display:configdirname"
  for variant in "VS Code:Code" "VS Code Insiders:Code - Insiders" "Cursor:Cursor"; do
    label="${variant%%:*}"
    dirname="${variant#*:}"
    case "$(uname -s)" in
      Darwin) base="$HOME/Library/Application Support/$dirname/User" ;;
      *)      base="${XDG_CONFIG_HOME:-$HOME/.config}/$dirname/User" ;;
    esac
    [ -d "$base" ] || continue          # variant not installed
    need jq || { warn "$label found but jq missing; skipping (install jq to auto-register)"; continue; }
    cfg="$base/mcp.json"
    [ -f "$cfg" ] || printf '{"servers":{}}' > "$cfg"
    if jq -e '.servers.runner != null' "$cfg" >/dev/null 2>&1; then
      ok "$label already has a runner entry -- left unchanged"
      continue
    fi
    entry="$(jq -n --arg n "$NODE_BIN" --arg e "$MCP_ENTRY" \
          '{type:"stdio", command:$n, args:[$e]}')"
    tmp="$cfg.tmp.$$"
    if jq --argjson r "$entry" '.servers.runner = $r' "$cfg" > "$tmp" 2>/dev/null && mv "$tmp" "$cfg"; then
      ok "Registered with $label ($cfg)"
    else
      rm -f "$tmp"
      warn "$label found but registration failed; add manually via: code --add-mcp '{\"name\":\"runner\",\"command\":\"$NODE_BIN\",\"args\":[\"$MCP_ENTRY\"]}'"
    fi
  done
}

# --- Uninstall ---------------------------------------------------------------
#
# Removes exactly what the installer put down:
#   - the install dir  $XDG_DATA_HOME/runner-mcp/  (payload + global index +
#     any global-fallback run dirs)
#   - the ~/.local/bin/{runner-cli,runnerlog} symlinks (only if they point
#     into our install dir -- never deletes an unrelated binary)
#   - the runner MCP registration from every supported agent (opencode,
#     Claude, Codex, Grok), even if the user customized it
#
# It NEVER touches per-project <git-root>/.runner/ run data -- that lives in
# your project trees, not in the install, and survives uninstall (and a clean
# reinstall). Dry-run by default; pass --yes to actually remove.

# Does a ~/.local/bin entry symlink into our install dir?
links_into_install() {
  link="$1"
  [ -L "$link" ] || return 1
  tgt="$(readlink "$link" 2>/dev/null || true)"
  case "$tgt" in
    "$INSTALL_DIR"/*) return 0 ;;
    *) return 1 ;;
  esac
}

unregister_opencode() {
  cfg="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
  [ -f "$cfg" ] || return 0
  need jq || { warn "opencode config present but jq missing; remove .mcp.runner from $cfg by hand"; return 0; }
  jq -e '.mcp.runner != null' "$cfg" >/dev/null 2>&1 || return 0
  if [ -n "$DO_REMOVE" ]; then
    tmp="$cfg.tmp.$$"
    jq 'del(.mcp.runner)' "$cfg" > "$tmp" && mv "$tmp" "$cfg" && ok "opencode: removed .mcp.runner"
  else
    info "would remove .mcp.runner from $cfg"
  fi
}

unregister_via_cli() {
  label="$1"; bin="$2"; shift 2
  need "$bin" || return 0
  # "$@" is the remove command's args after the binary.
  if [ -n "$DO_REMOVE" ]; then
    "$bin" "$@" >/dev/null 2>&1 && ok "$label: removed runner registration" \
      || info "$label: no runner registration to remove (or already gone)"
  else
    info "would run: $bin $*"
  fi
}

unregister_vscode() {
  for variant in "VS Code:Code" "VS Code Insiders:Code - Insiders" "Cursor:Cursor"; do
    label="${variant%%:*}"
    dirname="${variant#*:}"
    case "$(uname -s)" in
      Darwin) cfg="$HOME/Library/Application Support/$dirname/User/mcp.json" ;;
      *)      cfg="${XDG_CONFIG_HOME:-$HOME/.config}/$dirname/User/mcp.json" ;;
    esac
    [ -f "$cfg" ] || continue
    need jq || { warn "$label config present but jq missing; remove .servers.runner from $cfg by hand"; continue; }
    jq -e '.servers.runner != null' "$cfg" >/dev/null 2>&1 || continue
    if [ -n "$DO_REMOVE" ]; then
      tmp="$cfg.tmp.$$"
      jq 'del(.servers.runner)' "$cfg" > "$tmp" && mv "$tmp" "$cfg" && ok "$label: removed .servers.runner"
    else
      info "would remove .servers.runner from $cfg"
    fi
  done
}

uninstall() {
  printf '\n'
  step "Uninstalling $TOOL"
  [ -n "$DO_REMOVE" ] || warn "DRY RUN -- nothing will be removed. Re-run with --yes to proceed."
  printf '\n'

  # 1. install dir
  if [ -d "$INSTALL_DIR" ]; then
    if [ -n "$DO_REMOVE" ]; then rm -rf "$INSTALL_DIR" && ok "removed $INSTALL_DIR"
    else info "would remove $INSTALL_DIR (payload + global index + global-fallback runs)"; fi
  else
    info "install dir not found: $INSTALL_DIR"
  fi

  # 2. CLI symlinks (only if they point into our install dir)
  for name in runner-cli runnerlog; do
    link="$BIN_DIR/$name"
    if links_into_install "$link"; then
      if [ -n "$DO_REMOVE" ]; then rm -f "$link" && ok "removed symlink $link"
      else info "would remove symlink $link"; fi
    elif [ -e "$link" ] || [ -L "$link" ]; then
      warn "$link does not point into our install dir -- leaving it alone"
    fi
  done

  # 3. agent registrations
  unregister_opencode
  unregister_via_cli "Claude Code" claude mcp remove runner
  unregister_via_cli "Codex"       codex  mcp remove runner
  unregister_via_cli "Grok"        grok   mcp remove runner
  unregister_vscode

  printf '\n'
  if [ -n "$DO_REMOVE" ]; then
    ok "$TOOL uninstalled."
    info "Per-project .runner/ run data was left untouched."
  else
    warn "Dry run complete. Re-run with --yes to actually uninstall:"
    warn "    curl -fsSL <install.sh-url> | sh -s -- --uninstall --yes"
  fi
  printf '\n'
}

# --- Main --------------------------------------------------------------------
do_install() {
  printf '\n'
  step "Installing $TOOL v$RUNNER_MCP_VERSION"
  detect_platform
  info "Platform: $PLATFORM"
  check_deps
  fetch_payload
  link_clis
  register_agents
  printf '\n'
  ok "$TOOL v$RUNNER_MCP_VERSION installed to $INSTALL_DIR"
  info "CLIs: $BIN_DIR/runner-cli, $BIN_DIR/runnerlog"
  info "Restart your agent (opencode/claude/codex/grok/VS Code) to load the MCP server."
  printf '\n'
}

# Arg parsing: default action is install. --uninstall switches to removal;
# --yes arms the actual deletion (otherwise uninstall is a dry run).
MODE="install"
DO_REMOVE=""
for arg in "$@"; do
  case "$arg" in
    --uninstall|-u) MODE="uninstall" ;;
    --yes|-y)       DO_REMOVE="1" ;;
    --help|-h)
      printf 'Usage: install.sh [--uninstall] [--yes]\n'
      printf '  (no args)      install or upgrade runner-mcp\n'
      printf '  --uninstall    remove runner-mcp (dry run unless --yes)\n'
      printf '  --yes          confirm the uninstall removal\n'
      exit 0 ;;
    *) warn "ignoring unknown argument: $arg" ;;
  esac
done

case "$MODE" in
  uninstall) uninstall ;;
  *)         do_install ;;
esac
