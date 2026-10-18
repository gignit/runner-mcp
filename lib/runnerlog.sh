#!/usr/bin/env bash
# runnerlog.sh -- bash sourceable helpers for the runner protocol.
#
# Source this file at the top of your script (call `runner_helpers` via the
# MCP, or `runner-mcp helpers`, to get the exact absolute path for your
# install):
#     source "${XDG_DATA_HOME:-$HOME/.local/share}/runner-mcp/lib/runnerlog.sh"
#
# Functions:
#     runnerlog_section_start <name>
#     runnerlog_section_end   <name> ok|failed [exit=N] [reason="..."]
#     runnerlog_event         "<message>" [key=value ...]
#     runnerlog_metric        key=value [key=value ...]
#     runnerlog_fail          "<message>"   (also exits the script with code 1)
#
# Each function shells out to the `runnerlog` CLI which centralizes JSON
# serialization. No JSON construction in bash -- safe quoting always.

# Locate the CLI. Prefer PATH (after `make install` symlinks it), fall
# back to the source-tree location, then to the install dest.
_runnerlog_cli() {
    local cli
    if command -v runnerlog >/dev/null 2>&1; then
        echo "runnerlog"
        return
    fi
    # Source-tree fallback (when running from ~/src/utils/runner)
    cli="${BASH_SOURCE[0]%/*}/runnerlog"
    if [ -x "$cli" ]; then
        echo "$cli"
        return
    fi
    # Install dest fallback (XDG data dir; honors $XDG_DATA_HOME)
    cli="${XDG_DATA_HOME:-$HOME/.local/share}/runner-mcp/lib/runnerlog"
    if [ -x "$cli" ]; then
        echo "$cli"
        return
    fi
    echo "runnerlog: cannot locate runnerlog CLI on PATH or in standard install dirs" >&2
    return 1
}

# Run the CLI. On Windows (Git Bash / MSYS / Cygwin) the shebang's python3
# is often the Microsoft Store stub, so run the script with the Python that
# runner itself uses (runner exports it as RUNNER_PYTHON to every run).
_runnerlog() {
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*)
            "${RUNNER_PYTHON:-python}" "${BASH_SOURCE[0]%/*}/runnerlog" "$@" ;;
        *)
            "$(_runnerlog_cli)" "$@" ;;
    esac
}

runnerlog_section_start() {
    _runnerlog section_start "$@"
}

runnerlog_section_end() {
    _runnerlog section_end "$@"
}

runnerlog_event() {
    _runnerlog event "$@"
}

runnerlog_metric() {
    _runnerlog metric "$@"
}

runnerlog_fail() {
    _runnerlog fail "$@"
    exit 1
}
