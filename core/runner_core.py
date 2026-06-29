"""
runner_core - core library + CLI for the runner MCP service.

The runner spawns a script detached, captures stdout/stderr to log files,
and parses ::run:: marker lines from stdout into a structured event log.
Agents call MCP tools that consult the structured event log, NOT the raw
logs (though raw logs are available for "hop the fence" forensics).

This module is the single source of truth for:
  - storage layout (run_root, file names)
  - the wire-format protocol (::run:: prefix + JSON payload)
  - section/event/metric/fail vocab
  - the agent-friendly status response shape

The MCP server (TypeScript) shells out to this CLI for every tool call.

Design rules (keep them in mind when editing):
  - Section status values: "running", "ok", "failed", "unknown"
    "unknown" = section was open when script exited; we don't know if it
    would have succeeded.
  - Run state: "starting", "running", "exited"
  - Run result: null (still running), "success" (terminal & no failed
    sections & exit 0), "failed" (terminal & any failure or non-zero exit)
  - Top of status response is decision-fields-first; agents fixate on first
    fields per Buddy's panel review.
  - Delta-aware: every status call records `last_event_seen`; subsequent
    calls return only new events since that watermark + the current
    snapshot of decision fields.
  - stderr is opt-in noise: return only `stderr_new_count` by default; sample
    1-3 lines only when terminal-failed (per Buddy MVP cut).
"""

from __future__ import annotations

import argparse
import errno
import importlib
import json
import os
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Agent runtimes live alongside the other helpers in <install_root>/lib/agents.
# Loaded lazily so the rest of runner is unaffected if the package is missing
# (e.g. older installs without the multi-agent feature). The package exposes
# AGENT_REGISTRY {name: module} where each module has build_cmd() + extract().
_AGENTS_DIR = Path(__file__).resolve().parent.parent / "lib"


def _agents_module():
    """Return the lib.agents package, importing it lazily.

    Returns None if the package is missing or fails to import -- callers
    should treat that as "no agent runtimes available" rather than fatal.
    """
    if str(_AGENTS_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENTS_DIR))
    try:
        return importlib.import_module("agents")
    except Exception:
        return None

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Wire-format prefix. Must appear at column 0 of stdout. The rest of the line
# is a JSON object. See docs/GUIDE.md for the full protocol spec.
WIRE_PREFIX = "::run:: "

# Storage. Per-project (.runner inside a git repo) or global fallback.
#
# Per-project run data lives at <gitroot>/.runner/ -- it is project-scoped
# working data (like .git or node_modules), so it belongs in the project
# tree. This is what keeps each project's runs isolated: an agent only
# discovers runs under its own project root.
#
# The global fallback (used when the cwd is not inside a git repo) follows
# the XDG Base Directory spec: $XDG_DATA_HOME/runner-mcp, defaulting to
# ~/.local/share/runner-mcp on Linux and macOS.
RUNNER_DIRNAME = ".runner"


def _xdg_data_home() -> Path:
    x = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(x) if x else (Path.home() / ".local" / "share")


RUNNER_GLOBAL = _xdg_data_home() / "runner-mcp"

# Global runId -> runRoot index at <RUNNER_GLOBAL>/index.jsonl, one JSON
# line per run. Lets `runner_status`, `runner_section`, `runner_grep` etc.
# resolve a runId without knowing the original cwd. Append-only; we walk
# from the end on lookup so the latest registration wins.
RUNNER_INDEX_FILE = RUNNER_GLOBAL / "index.jsonl"

# Per-run files inside <run_root>/<runId>/
FILE_META = "meta.json"
FILE_STDOUT = "stdout.log"
FILE_STDERR = "stderr.log"
FILE_EVENTS = "events.jsonl"
FILE_TRACKER = "tracker.json"   # per-(agent,run) last_event_seen + last_stderr_line
FILE_PID = "pid"

# Stall threshold: if no event arrives for this long, status reports
# stalled_for_sec > 0 so the agent can decide to investigate.
STALL_HEARTBEAT_SEC = 0  # 0 = always report; agent decides what's "stalled"

# Default poll cadence the runner suggests via pollAfterSec
DEFAULT_POLL_SEC = 15

# Default count of new stderr lines sampled on terminal-failed run
STDERR_SAMPLE_LIMIT = 3

# Default count of recent events returned in `runner_section` logTail
SECTION_LOG_TAIL = 10
SECTION_STDERR_TAIL = 10

# Recognized section_end status values
SECTION_STATUSES = {"ok", "failed", "unknown"}

# Recognized event verbs
EVENT_VERBS = {"section_start", "section_end", "event", "metric", "fail"}


# -----------------------------------------------------------------------------
# Storage discovery
# -----------------------------------------------------------------------------

def find_run_root(cwd: Path | None = None) -> Path:
    """Find the runner storage root for the given working directory.

    Walks up from cwd looking for .git; uses <git_root>/.runner if found.
    Falls back to the global storage dir (RUNNER_GLOBAL, the XDG data dir)
    otherwise.

    When a git root is found, also ensures `.runner` is in
    `<git_root>/.git/info/exclude` so the run-storage directory does not
    show up in `git status` for the host project. We use info/exclude
    (local-only) instead of writing to .gitignore (tracked) so the host
    project's git history isn't polluted by the runner.
    """
    start = cwd if cwd is not None else Path.cwd()
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".git").exists():
            root = cur / RUNNER_DIRNAME
            root.mkdir(parents=True, exist_ok=True)
            _ensure_in_git_exclude(cur, RUNNER_DIRNAME)
            return root
        cur = cur.parent
    RUNNER_GLOBAL.mkdir(parents=True, exist_ok=True)
    return RUNNER_GLOBAL


def _ensure_in_git_exclude(git_root: Path, entry: str) -> None:
    """Add `entry` to <git_root>/.git/info/exclude if not already present.

    Best-effort: silently no-ops on any error (we never want a missing
    exclude file to block a run).
    """
    git_exclude = git_root / ".git" / "info" / "exclude"
    if not git_exclude.parent.exists():
        return
    try:
        if not git_exclude.exists():
            git_exclude.parent.mkdir(parents=True, exist_ok=True)
            git_exclude.touch()
        try:
            content = git_exclude.read_text(encoding="utf-8")
        except OSError:
            content = ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == entry or stripped == "/" + entry:
                return
        with git_exclude.open("a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(entry + "\n")
    except OSError:
        pass


def run_dir(run_root: Path, run_id: str) -> Path:
    return run_root / run_id


def _index_register(run_id: str, run_dir_path: Path) -> None:
    """Append a runId -> runRoot record to the global index.

    Idempotent in effect: lookups walk from the bottom so a re-registration
    (same id, new path) shadows older entries.
    """
    try:
        RUNNER_GLOBAL.mkdir(parents=True, exist_ok=True)
        with RUNNER_INDEX_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "runId": run_id,
                "runDir": str(run_dir_path),
                "registeredAt": int(time.time()),
            }) + "\n")
    except OSError:
        # Index is best-effort; don't fail the spawn if we can't write it.
        pass


def _index_lookup(run_id: str) -> Path | None:
    """Return the most-recent runDir registered for this runId, or None."""
    if not RUNNER_INDEX_FILE.exists():
        return None
    try:
        with RUNNER_INDEX_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("runId") == run_id:
            d = Path(rec.get("runDir", ""))
            if d.exists():
                return d
            # Stale entry; keep walking older entries
    return None


# -----------------------------------------------------------------------------
# UUIDv7 (time-sortable run IDs)
# -----------------------------------------------------------------------------

def gen_run_id() -> str:
    """Generate a UUIDv7 (time-sortable). RFC 9562."""
    ts_ms = int(time.time() * 1000)
    rand = uuid.uuid4().bytes[6:]
    b = ts_ms.to_bytes(6, "big") + bytes([0x70 | (rand[0] & 0x0f)]) + rand[1:]
    return str(uuid.UUID(bytes=b))


# Pre-compiled UUIDv7 shape check used by cmd_agent to disambiguate
# runner runIds from backend session ids. UUIDv7 has version nibble 7
# at char 14 and variant nibble 8/9/a/b at char 19 (RFC 9562).
_UUIDV7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_runner_run_id(s: str) -> bool:
    """Return True iff s matches the UUIDv7 shape runner issues.

    Used to disambiguate: in cmd_agent, the `--run-id` parameter accepts
    EITHER a runner-issued runId (UUIDv7) OR a backend session id to
    adopt (e.g. opencode 'ses_...'). Anything not matching this shape
    is treated as a backend session id by the adoption codepath.

    The check is intentionally tight (full UUIDv7 spec, not just dashes)
    so that an agent typing a garbled runId fails cleanly instead of
    accidentally being treated as a session id and spawning a new
    conversation.
    """
    return bool(_UUIDV7_RE.match(s or ""))


# -----------------------------------------------------------------------------
# Meta + atomic JSON writes
# -----------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: temp file then rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# -----------------------------------------------------------------------------
# Spawn
# -----------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    """Spawn a script detached, return JSON {runId, pid, name, startedAt, runRoot}.

    The double-fork-and-setsid pattern detaches the spawned process so it
    survives the runner CLI's exit. The script's stdout/stderr go to
    log files; it gets its own session so we can kill -9 the process group
    later if needed.

    With --blocking (default), the parent process polls status every
    BLOCKING_POLL_SEC after spawning and returns the final status snapshot
    when the run is terminal or when BLOCKING_WAIT_SEC elapses. The wait
    is fixed (NOT user-tunable) because the MCP transport caps requests
    at ~60s and agents can't raise that. The job is NEVER killed -- on
    elapse the response sets stillRunning=true and the agent follows up
    with runner_status using the runId.
    """
    if not args.cmd:
        print(json.dumps({"error": "cmd required"}), file=sys.stderr)
        return 2

    # Run storage is ALWAYS scoped to the agent's project root (the git
    # repo containing the runner CLI's cwd, i.e. the agent's session
    # cwd). It is NOT derived from args.cwd -- args.cwd is the spawn
    # working directory for the cmd itself and may point at an
    # unrelated project. Keeping storage tied to the agent's project
    # root means an agent's `runner_list` only ever shows runs that
    # belong to its own session, even when those runs target cmds in
    # other projects.
    run_root = find_run_root(None)
    run_id = gen_run_id()
    rdir = run_dir(run_root, run_id)
    rdir.mkdir(parents=True, exist_ok=True)

    # Gate (do NOT rewrite) commands that pipe into filter/pager tools.
    #
    # Why gate instead of silently rewriting: mutating the agent's command
    # and then EXECUTING the mutation is unsafe -- a mis-parse could turn a
    # benign-looking command into a destructive one the agent never wrote
    # and cannot see. So when trailing filter pipes are detected we run
    # NOTHING and return a positive, instructional message telling the agent
    # how to re-issue the command correctly (and pointing at runner_helpers
    # for multi-step work). Pass noScrub:true to bypass and run verbatim.
    raw_cmd = args.cmd
    if not getattr(args, "no_scrub", False):
        analysis = analyze_command(raw_cmd)
        if analysis["shouldGate"]:
            # Reject without spawning. rdir was created above; remove it so we
            # don't leave an empty run dir behind for a run that never started.
            try:
                import shutil
                shutil.rmtree(rdir, ignore_errors=True)
            except Exception:
                pass
            print(json.dumps(
                _build_command_gate(raw_cmd, analysis),
                indent=2 if getattr(args, "pretty", False) else None,
            ))
            return 2
    cmd_to_run = raw_cmd

    started_at = int(time.time())
    # Resolve a unique name. Auto-derived names always get a -NNNN suffix
    # so common cmds (go test, make reinstall) don't all collide as "go-test".
    # Explicit names are only suffixed if they collide with existing runs in
    # the same project root -- otherwise the agent's chosen name is honored
    # exactly. Allows runner_list filtering to see distinct runs even when
    # 20+ go-test runs were issued in a session.
    name_explicit = bool(args.name)
    base_name = args.name or _derive_name(cmd_to_run)
    name = _next_unique_name(run_root, base_name, always_suffix=not name_explicit)
    parser_choice = getattr(args, "parser", None) or "auto"

    # Pre-write meta so status calls work immediately, even before the
    # spawned process has produced anything.
    description = getattr(args, "description", None) or None
    meta: dict[str, Any] = {
        "runId": run_id,
        "name": name,
        "description": description,
        "cmd": cmd_to_run,
        "cwd": args.cwd or os.getcwd(),
        "startedAt": started_at,
        "endedAt": None,
        "exitCode": None,
        "state": "starting",
        "result": None,
        "fatalMsg": None,
        "killedAt": None,
        "runRoot": str(rdir),
        "parser": parser_choice,
        "restartCount": 0,
        # Records whether this was started in blocking mode. runner_status
        # uses this to decide whether to auto-wait when the run is still
        # active (blocking runs => wait; non-blocking services => return
        # immediately). Lets the agent's protocol be "keep calling
        # runner_status until terminal" without needing a separate wait
        # flag for every call.
        "blockingMode": bool(getattr(args, "blocking", False)),
    }
    _atomic_write_json(rdir / FILE_META, meta)

    pid = _spawn_into(rdir, cmd_to_run, args.cwd, name, run_id)
    if pid < 0:
        print(json.dumps({"error": "spawn failed"}), file=sys.stderr)
        return 1

    blocking = bool(getattr(args, "blocking", False))
    start_payload: dict[str, Any] = {
        "runId": run_id,
        "pid": pid,
        "name": name,
        "startedAt": started_at,
        "runRoot": str(rdir),
        "blocking": blocking,
    }

    if not blocking:
        print(json.dumps(start_payload))
        return 0

    return _block_until_terminal(rdir, start_payload, args.pretty if hasattr(args, "pretty") else False)


def _set_pty_winsize(fd: int, rows: int = 50, cols: int = 200) -> None:
    """Set the window size on a pty slave fd.

    Programs that consult the terminal width (COLUMNS / ioctl TIOCGWINSZ) to
    decide where to wrap -- pterm tables, `go test` progress, column(1) --
    otherwise assume 80 columns on a fresh pty and hard-wrap the captured
    log. A wide window keeps the captured output close to what a real wide
    terminal would show.
    """
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    import fcntl  # local import: only needed on the pty spawn path

    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _spawn_into(rdir: Path, cmd: str, cwd: str | None, name: str, run_id: str) -> int:
    """Double-fork-and-setsid spawn into an existing run dir.

    Shared by cmd_start and cmd_restart. Returns the spawned PID (parent's
    perspective) or -1 on failure. Blocks the parent only briefly while the
    PID file is written by the daemon.
    """
    stdout_path = rdir / FILE_STDOUT
    stderr_path = rdir / FILE_STDERR
    meta_path = rdir / FILE_META
    pid_path = rdir / FILE_PID

    # Touch the log files so they exist even if the script writes nothing.
    # (No events.jsonl: events are parsed on demand from stdout.log plus
    # adapter synthesis; the FILE_EVENTS constant is retained only to clean
    # up any legacy file during restart.)
    stdout_path.touch()
    stderr_path.touch()
    # Clear stale pid file so the parent's wait-for-pid loop sees the new write
    if pid_path.exists():
        pid_path.unlink()

    # Register in the global runId -> runDir index so future MCP calls can
    # resolve this run without knowing the original cwd.
    _index_register(run_id, rdir)

    # Double-fork to detach
    if os.fork() > 0:
        # Parent: wait briefly for child to write PID so we can return it.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if pid_path.exists():
                break
            time.sleep(0.05)
        pid_str = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else ""
        try:
            return int(pid_str)
        except ValueError:
            return -1

    # First child
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    # Grandchild: detached daemon
    sys.stdin.close()
    sys.stdout.flush()
    sys.stderr.flush()

    env = dict(os.environ)
    env["RUNNER_RUN_ID"] = run_id
    env["RUNNER_RUN_ROOT"] = str(rdir)

    # Run the child under PSEUDO-TERMINALS so its (and its whole descendant
    # tree's) stdout/stderr are line-buffered instead of block-buffered.
    #
    # Why: libc / Go / ssh / most programs switch stdout to FULL block
    # buffering (4-8 KiB) when it is NOT a tty -- e.g. a plain log file. The
    # effect is that output for long-running children (a remote `go build`,
    # a test suite, an ssh session) accumulates in the child's internal
    # buffer and only reaches stdout.log in big bursts (often only at exit),
    # so the live tail (runner-mcp TUI, runner_status delta) shows nothing for
    # long stretches. Giving the child a tty makes it line-buffer, so each
    # line lands in stdout.log immediately and the live view stays current.
    #
    # We allocate TWO ptys (stdout + stderr) so the two streams stay
    # separate in their respective log files, and pump master -> file in a
    # select loop until both close. Buffering at the file layer is line-
    # flushed below.
    out_log = open(str(stdout_path), "ab", buffering=0)
    err_log = open(str(stderr_path), "ab", buffering=0)

    out_master, out_slave = pty.openpty()
    err_master, err_slave = pty.openpty()
    # Best-effort: widen the pty so programs that query COLUMNS don't hard-
    # wrap output at 80 cols in the captured log.
    try:
        _set_pty_winsize(out_slave)
        _set_pty_winsize(err_slave)
    except Exception:
        pass

    devnull = os.open(os.devnull, os.O_RDONLY)
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdin=devnull,
        stdout=out_slave,
        stderr=err_slave,
        env=env,
        cwd=cwd or None,
        executable="/bin/bash",
        close_fds=True,
    )
    # The slave ends belong to the child now; close ours so the masters see
    # EOF when the child exits.
    os.close(out_slave)
    os.close(err_slave)
    os.close(devnull)

    pid_path.write_text(str(proc.pid))
    meta = _read_json(meta_path) or {}
    meta["pid"] = proc.pid
    meta["state"] = "running"
    _atomic_write_json(meta_path, meta)

    # Pump both pty masters -> log files until EOF on both. PTY masters
    # deliver data as the child writes lines, so the log files grow live.
    open_masters = {out_master: out_log, err_master: err_log}
    while open_masters:
        try:
            ready, _, _ = select.select(list(open_masters.keys()), [], [])
        except (OSError, ValueError):
            break
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                # PTY master read raises EIO when the slave side has fully
                # closed -- treat as EOF for this stream.
                chunk = b""
            if not chunk:
                try:
                    open_masters[fd].flush()
                except Exception:
                    pass
                os.close(fd)
                del open_masters[fd]
                continue
            try:
                open_masters[fd].write(chunk)
                open_masters[fd].flush()
            except Exception:
                pass

    try:
        out_log.flush()
        err_log.flush()
    except Exception:
        pass

    rc = proc.wait()
    ended_at = int(time.time())
    meta = _read_json(meta_path) or meta
    meta["endedAt"] = ended_at
    meta["exitCode"] = rc
    meta["state"] = "exited"

    try:
        events = parse_events(stdout_path, parser_hint=meta.get("parser", "auto"))
        sections = build_sections(events)
        any_failed = any(s.status == "failed" for s in sections)
        any_open = any(s.status == "running" for s in sections)
    except Exception:
        any_failed = False
        any_open = False
    if rc != 0 or any_failed or any_open:
        meta["result"] = "failed"
    else:
        meta["result"] = "success"
    _atomic_write_json(meta_path, meta)

    os._exit(0)


# Blocking-mode wait: 540s (9 min) for ALL blocking runs (sub-agent
# turns, builds, tests, services, anything).
#
# WHAT THIS IS: an upper bound on how long ONE MCP call holds before
# returning a status snapshot. NOT a timeout on the run itself.
#
# WHAT THIS IS NOT: this never kills the spawned process. A run lives
# until it exits naturally, errors out, or the agent calls runner_kill
# -- never because BLOCKING_WAIT_SEC elapsed. When the wait elapses,
# the poll loop returns a `stillRunning: true` response and the spawned
# process keeps going completely untouched. The agent calls the same
# tool again to resume blocking. Same protocol as before; only the
# default wait duration changed.
#
# Why 540: the installer registers the runner MCP server with
# timeout=600000 (10 min) in opencode's config, so the MCP transport
# holds calls for that long. 9 min < 10 min leaves headroom for
# response serialize + transport overhead at the boundary. The poll
# loop returns IMMEDIATELY when the run goes terminal, so a 5s build
# returns in 5s, not 9 min. The long wait only matters when the run
# is genuinely long-running, in which case holding the call saves the
# calling agent from poll-bombing.
BLOCKING_WAIT_SEC = 540
# Poll cadence is ADAPTIVE -- see _next_poll_interval below. Constant
# kept for the few external callers that reference it; new code should
# rely on the helper. Rationale: a fixed 15s poll meant a 0.5s build
# took 15s to return (one full sleep before the first state check).
# Fast initial polling catches short runs immediately; slower polling
# kicks in only once we know the run is long-lived.
BLOCKING_POLL_SEC = 15


def _next_poll_interval(elapsed_sec: float) -> float:
    """Return how long to sleep before the next state check.

    Cadence schedule:
      first  5s:  every 0.5s   (catch short builds immediately)
      next  55s:  every 2s     (catch medium runs without much overhead)
      after 60s:  every 15s    (long runs -- cheap polling is enough)

    All values are well below the BLOCKING_WAIT_SEC deadline, so a poll
    will land before the wait elapses regardless of when in the
    schedule we are.
    """
    if elapsed_sec < 5:
        return 0.5
    if elapsed_sec < 60:
        return 2.0
    return 15.0


def _block_until_terminal(
    rdir: Path,
    start_payload: dict[str, Any],
    pretty: bool,
) -> int:
    """Parent-side poll loop for blocking mode.

    Sleeps in BLOCKING_POLL_SEC increments, reads meta + events, and
    returns when the run is terminal or BLOCKING_WAIT_SEC elapses.
    Never kills the job -- on elapse the response sets
    stillRunning=true and the agent follows up (via runner_status for
    regular runs, or via the dispatch tool with just runId for
    sub-agent runs -- see _attach_still_running).

    The loop returns IMMEDIATELY when the run goes terminal -- a 1s
    build returns in 1s, not 9 min. The long wait window only matters
    for genuinely long-running operations where holding the MCP call
    saves the calling agent from poll-bombing.
    """
    start = time.time()
    deadline = start + BLOCKING_WAIT_SEC
    meta_path = rdir / FILE_META
    while True:
        time.sleep(_next_poll_interval(time.time() - start))
        meta = _read_json(meta_path, default={}) or {}
        events = parse_events(rdir / FILE_STDOUT, parser_hint=meta.get("parser", "auto"))
        sections = build_sections(events)
        synth = synthesize_run_state(meta, sections)
        terminal = synth["state"] == "exited"

        # Agent-mode early exit: for sub-agent runs, the wrapping
        # process may not have died yet (opencode sometimes hangs
        # retrying a rate-limited request) but we already have enough
        # signal to call this turn done. Peek the per-runtime view --
        # if finalReply OR interrupted are present, the turn has
        # reached its outcome and there's no value in waiting longer.
        # We force terminal=True so _build_status_response shapes the
        # response as the focused finalReply / interrupted form.
        #
        # On interrupted (rate-limited / wedged opencode retrying):
        # we ALSO kill the spawned process group. The turn is dead;
        # leaving the process around just burns provider quota on
        # retries that will all fail the same way. The conversation
        # state is preserved on the backend (session id is intact)
        # so the agent can dispatch 'continue' to resume cleanly.
        #
        # On finalReply: we do NOT kill. The process is finishing
        # its normal exit path (rendering, fd cleanup) and will be
        # gone within milliseconds on its own.
        if not terminal and meta.get("agentRuntime"):
            agents_mod = _agents_module()
            runtime = agents_mod.get(meta["agentRuntime"]) if agents_mod else None
            if runtime is not None:
                try:
                    # compact_view with terminal=True so its internal
                    # interrupted gate doesn't suppress the signal --
                    # we ARE asking about terminal state here.
                    peek = runtime.compact_view(rdir, terminal=True)
                except Exception:
                    peek = None
                if peek and peek.get("interrupted"):
                    # Kill the wedged process group, then refresh
                    # meta/synth so the response reflects the kill.
                    meta = _kill_run_pgroup(rdir, meta)
                    synth = synthesize_run_state(meta, sections)
                    terminal = True
                elif peek and peek.get("finalReply"):
                    terminal = True

        if terminal or time.time() >= deadline:
            # Build the final response: a status snapshot plus the start info
            response = _build_status_response(
                meta=meta,
                sections=sections,
                synth=synth,
                new_events=events,
                cursor_line=0,
                new_cursor_line=events[-1].line_no if events else 0,
                new_stderr_count=_count_lines(rdir / FILE_STDERR),
                rdir=rdir,
                verbose=False,
            )
            # On elapse, surface the "still running" hint. The follow-up
            # protocol differs by run kind: regular runs poll
            # runner_status (auto-waits same window); sub-agent runs
            # poll the dispatch tool with just runId.
            # _attach_still_running picks the right hint based on
            # meta.agentRuntime.
            if not terminal:
                _attach_still_running(response, meta)
            print(json.dumps(response, indent=2 if pretty else None))
            return 0


def cmd_restart(args: argparse.Namespace) -> int:
    """Restart an existing run: kill the current process group, wipe state,
    and re-spawn the same cmd/cwd/name under the same runId.

    Designed for managing long-running services (dev servers, watchers,
    file pollers) where the agent wants to refresh the process without
    juggling a new runId. Wipes stdout/stderr/events/tracker so a fresh
    run starts clean. Bumps meta.restartCount.
    """
    rdir = _resolve_run_dir(args)
    if rdir is None:
        return 1
    meta = _read_json(rdir / FILE_META, default={})
    if not meta:
        print(json.dumps({"error": f"run {args.run_id} has no meta.json"}), file=sys.stderr)
        return 1
    cmd = meta.get("cmd")
    cwd = meta.get("cwd")
    name = meta.get("name", "run")
    run_id = meta["runId"]
    if not cmd:
        print(json.dumps({"error": "meta has no cmd to re-run"}), file=sys.stderr)
        return 1
    # Already-scrubbed cmd is in meta; nothing more to do (re-scrubbing is a
    # no-op on cleaned input).

    # Kill existing PID group if alive
    pid = meta.get("pid", 0) or 0
    killed = False
    if pid > 0 and _process_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            killed = True
        except (ProcessLookupError, PermissionError):
            pass
        # Brief wait for OS reap
        for _ in range(20):
            if not _process_alive(pid):
                break
            time.sleep(0.05)

    # Wipe state
    for f in (FILE_STDOUT, FILE_STDERR, FILE_EVENTS, FILE_TRACKER, FILE_PID):
        p = rdir / f
        if p.exists():
            try:
                p.unlink()
            except OSError:
                # Truncate as fallback (e.g. Windows lock)
                try:
                    p.write_text("")
                except OSError:
                    pass

    # Reset meta but preserve identity + restart history
    restart_count = int(meta.get("restartCount", 0)) + 1
    started_at = int(time.time())
    parser_choice = meta.get("parser", "auto")
    new_meta: dict[str, Any] = {
        "runId": run_id,
        "name": name,
        "description": meta.get("description"),
        "cmd": cmd,
        "cwd": cwd,
        "startedAt": started_at,
        "endedAt": None,
        "exitCode": None,
        "state": "starting",
        "result": None,
        "fatalMsg": None,
        "killedAt": None,
        "runRoot": str(rdir),
        "parser": parser_choice,
        "restartCount": restart_count,
        "previousEndedAt": meta.get("endedAt"),
        "previousExitCode": meta.get("exitCode"),
        "previousResult": meta.get("result"),
    }
    _atomic_write_json(rdir / FILE_META, new_meta)

    new_pid = _spawn_into(rdir, cmd, cwd, name, run_id)
    if new_pid < 0:
        print(json.dumps({"error": "respawn failed"}), file=sys.stderr)
        return 1

    payload = {
        "runId": run_id,
        "pid": new_pid,
        "name": name,
        "restartedAt": started_at,
        "restartCount": restart_count,
        "killedPreviousPid": killed,
        "runRoot": str(rdir),
    }
    print(json.dumps(payload))
    return 0


# Tools treated as pure output-filters when they appear AFTER a pipe.
_FILTER_TOOLS = {
    "head", "tail", "grep", "egrep", "fgrep", "rg", "ack",
    "less", "more", "wc", "cat", "awk", "sed", "cut", "sort", "uniq",
    "tee", "column", "fold", "fmt", "tr",
}


def analyze_command(cmd: str) -> dict[str, Any]:
    """Inspect a command WITHOUT modifying it.

    Returns a dict describing the shell patterns present so cmd_start can
    decide whether to gate the command and how to guide the agent:

        {
          "filterPipes": ["grep", "tail"],   # trailing pipe-to-filter tools
          "logicCount": 2,                    # number of &&/||/; -joined cmds
          "multiStep": True,                  # logicCount >= 2
          "shouldGate": True,                 # filterPipes OR multiStep
        }

    We never execute or rewrite; this is detection only.
    """
    result: dict[str, Any] = {
        "filterPipes": [], "logicCount": 1, "multiStep": False, "shouldGate": False,
    }
    if not cmd or not cmd.strip():
        return result

    # Minimal quote-aware splitter (detection only -- never mutates).
    def split_top(s: str, twochar: set[str], onechar: set[str]) -> list[str]:
        out: list[str] = []
        buf: list[str] = []
        in_s = in_d = False
        i = 0
        while i < len(s):
            c = s[i]
            if c == "'" and not in_d:
                in_s = not in_s; buf.append(c)
            elif c == '"' and not in_s:
                in_d = not in_d; buf.append(c)
            elif c == "\\" and i + 1 < len(s):
                buf.append(c); buf.append(s[i + 1]); i += 2; continue
            elif not in_s and not in_d and i + 1 < len(s) and (s[i] + s[i + 1]) in twochar:
                out.append("".join(buf)); buf = []; i += 2; continue
            elif not in_s and not in_d and c in onechar:
                out.append("".join(buf)); buf = []; i += 1; continue
            else:
                buf.append(c)
            i += 1
        out.append("".join(buf))
        return out

    def first_tok(seg: str) -> str:
        try:
            toks = shlex.split(seg.strip(), posix=True)
        except ValueError:
            return ""
        for t in toks:
            if "=" in t and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
                continue
            return os.path.basename(t)
        return ""

    logic_parts = split_top(cmd, {"&&", "||"}, {";"})
    logic_parts = [p for p in logic_parts if p.strip()]
    result["logicCount"] = len(logic_parts)
    result["multiStep"] = len(logic_parts) >= 2

    filters: list[str] = []
    for part in logic_parts:
        pipe_segs = split_top(part, set(), {"|"})
        pipe_segs = [s for s in pipe_segs if s.strip()]
        for seg in pipe_segs[1:]:  # segments AFTER the producer
            tok = first_tok(seg)
            if tok in _FILTER_TOOLS:
                filters.append(tok)
    result["filterPipes"] = filters

    result["shouldGate"] = bool(filters) or result["multiStep"]
    return result


def _runnerlog_lib_dir() -> Path:
    """Absolute path to the installed lib/ dir (where runnerlog.sh lives)."""
    return Path(__file__).resolve().parent.parent / "lib"


def _build_command_gate(cmd: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """Build the positive, instructional rejection payload for a gated cmd.

    We do NOT run anything. The message ENCOURAGES runner use, explains why
    the pattern is gated, and shows the agent the productive path: write a
    small instrumented script under <project>/.runner/scripts/ using the
    runnerlog helpers, then run THAT. Framed so the agent sees this as
    easier than perfectly escaping a compound one-liner -- not as a scolding.
    """
    run_root = find_run_root(None)
    scripts_dir = run_root / "scripts"
    lib_sh = _runnerlog_lib_dir() / "runnerlog.sh"
    filters = analysis.get("filterPipes") or []
    multi = analysis.get("multiStep")

    if filters and not multi:
        why = (
            f"Your command pipes into filter/pager tool(s) "
            f"({', '.join(sorted(set(filters)))}). runner already captures the "
            f"FULL output and gives you runner_grep / runner_section / the auto "
            f"stdoutTail, so the pipe both hides output from runner's adapters "
            f"and isn't needed."
        )
        fix = (
            "Re-run just the producer command (drop the trailing filter pipe), "
            "then filter the captured output with runner_grep or runner_section."
        )
    else:
        why = (
            f"Your command chains {analysis.get('logicCount')} steps with "
            f"&& / || / ; . Run as a one-liner you lose per-step visibility "
            f"(which step failed, how long each took) and you have to perfectly "
            f"escape quotes/pipes across the whole thing."
        )
        fix = (
            "For multi-step work, write a small script instead -- it's far more "
            "productive and reliable than escaping a compound one-liner, and "
            "runner gives you structured per-step status for free."
        )

    return {
        "error": "command gated (not run): use runner the productive way",
        "encouragement": (
            "You're using the right tool -- runner is the encouraged way to run "
            "builds, tests, servers, and scripts (don't fall back to raw bash). "
            "This is just the correct usage pattern."
        ),
        "why": why,
        "fix": fix,
        "scriptWorkflow": {
            "note": (
                "Put reusable/one-off runner scripts in this project's "
                ".runner/scripts/ directory. It's already git-excluded (no "
                "project noise), lives next to your runs, and you can copy / "
                "reuse / enhance these scripts later. Instrument them with the "
                "runnerlog helpers so each step reports structured status "
                "(section_start/end, metric, event) -- much better than parsing "
                "raw stdout, and easier than escaping everything by hand."
            ),
            "scriptsDir": str(scripts_dir),
            "suggestedPath": str(scripts_dir / "task.sh"),
            "template": (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f'source "{lib_sh}"\n'
                "\n"
                'runnerlog_section_start "step1"\n'
                "# ... first command ...\n"
                'runnerlog_section_end "step1" ok exit=$?\n'
                "\n"
                'runnerlog_section_start "step2"\n'
                "# ... next command ...\n"
                'runnerlog_section_end "step2" ok exit=$?\n'
            ),
            "thenRun": "runner_start { cmd: \"bash .runner/scripts/task.sh\" }",
            "helpersTool": (
                "Call runner_helpers for ready-to-paste bash/python/CLI "
                "instrumentation snippets and exact paths."
            ),
        },
        "escapeHatch": (
            "If you really want runner to execute this exact string verbatim "
            "(pipes/chains and all), pass noScrub: true."
        ),
        "yourCommand": cmd,
    }


# Suffix encoding: 4-character slots, monotonic-from-max, never reuses
# gaps. Format keeps width=4 forever by widening the alphabet when the
# decimal range is exhausted:
#
#   index  0..9999     -> "0000".."9999"
#   index 10000..10999 -> "000A".."999A"     (last slot = A, first 3 are 000-999)
#   index 11000..11999 -> "000B".."999B"
#   ...
#   index 35000..35999 -> "000Z".."999Z"     (35999 is the max -- ~36k values)
#
# After Z is exhausted there are no more 4-char codes; the agent gets a
# fallback monotonic decimal ("ABCD-36000") at that point. ~36k runs per
# base name in one project should be more than enough; if not, a wider
# scheme (alphanumeric all four positions) gives 1.6M more values.
#
# Why this layout: keeps the sort order chronological (0000 < 0001 < ...
# < 9999 < 000A < 001A) and keeps every name exactly 4 chars so columns
# in runner_list line up.

_SUFFIX_DECIMAL_CAP = 10000           # 0000..9999
_SUFFIX_LETTER_BLOCK = 1000           # 000X..999X per letter
_SUFFIX_LETTER_CAP = _SUFFIX_DECIMAL_CAP + 26 * _SUFFIX_LETTER_BLOCK   # 36000


def _encode_suffix(index: int) -> str:
    """Encode a non-negative index into the 4-char suffix scheme."""
    if index < 0:
        index = 0
    if index < _SUFFIX_DECIMAL_CAP:
        return f"{index:04d}"
    over = index - _SUFFIX_DECIMAL_CAP
    if over < 26 * _SUFFIX_LETTER_BLOCK:
        letter_idx, head = divmod(over, _SUFFIX_LETTER_BLOCK)
        letter = chr(ord("A") + letter_idx)
        return f"{head:03d}{letter}"
    # Beyond 35999: fall back to plain decimal (rare; ~36k+ runs of one
    # base name in a single project).
    return str(index)


def _decode_suffix(s: str) -> int | None:
    """Decode a suffix string back to its index, or None if not a known
    encoding."""
    if not s:
        return None
    # Pure decimal (covers 0000..9999 and the fallback large numbers)
    if s.isdigit():
        return int(s)
    # 3-digit head + uppercase letter trailer
    if len(s) == 4 and s[:3].isdigit() and s[3].isupper() and "A" <= s[3] <= "Z":
        head = int(s[:3])
        letter_idx = ord(s[3]) - ord("A")
        return _SUFFIX_DECIMAL_CAP + letter_idx * _SUFFIX_LETTER_BLOCK + head
    return None


def _next_unique_name(run_root: Path, base_name: str, always_suffix: bool) -> str:
    """Resolve a unique run name within the given project run root.

    `base_name` is the desired name (either explicit from the agent or
    auto-derived from the cmd).

    `always_suffix=True` (auto-derived) always appends a 4-char counter
    suffix so multiple runs of the same cmd are individually addressable
    (`go-test-0000`, `go-test-0001`, ...). The counter is monotonic from
    the existing max -- gaps are NEVER reused. If a session has
    go-test-0123 and 0000-0122 were purged, the next run is go-test-0124.
    This preserves chronological ordering.

    `always_suffix=False` (explicit name) honors the agent's chosen name
    exactly when no collision exists. On collision (or when -NNNN
    siblings exist), suffixes with the monotonic-from-max rule.

    Suffix encoding (see _encode_suffix): 0000-9999 then 000A-999Z.
    Stays exactly 4 chars across the first 36000 runs per base name.
    """
    existing: set[str] = set()
    max_index: int = -1
    suffix_re = re.compile(r"^" + re.escape(base_name) + r"-(\S+)$")
    if run_root.exists():
        for d in run_root.iterdir():
            if not d.is_dir():
                continue
            meta = _read_json(d / FILE_META)
            if not (meta and isinstance(meta.get("name"), str)):
                continue
            existing_name = meta["name"]
            existing.add(existing_name)
            m = suffix_re.match(existing_name)
            if m:
                idx = _decode_suffix(m.group(1))
                if idx is not None and idx > max_index:
                    max_index = idx

    # Explicit names: honor exactly when no collision and no suffixed siblings.
    if not always_suffix and base_name not in existing and max_index < 0:
        return base_name

    next_idx = max_index + 1 if max_index >= 0 else 0
    return f"{base_name}-{_encode_suffix(next_idx)}"


def _derive_name(cmd: str) -> str:
    """Derive a default human label from the command.

    Skips no-op shell prefixes that aren't representative of what the
    command is actually doing -- agents commonly write
    `cd /path && go test ./...` and we'd rather call that "go-test"
    than "cd". We unwrap:
      - `cd <dir> && <real cmd>`
      - leading env-var assignments (FOO=bar baz)
      - `bash -c "<real cmd>"` / `sh -c "..."`
    Then take the first 1-2 tokens of the real cmd, joined by `-`,
    so `go test ./...` -> `go-test`, `make reinstall` -> `make-reinstall`.
    """
    if not cmd:
        return "run"

    SKIP_PREFIXES = {"sudo", "time", "nice", "ionice", "exec", "command"}
    SHELL_DASH_C = {"bash", "sh", "zsh", "dash"}

    def first_real_segment(s: str) -> tuple[list[str], str]:
        # Walk past `cd <dir> &&`, `bash -c "..."`, env-var prefixes, and
        # other no-op shell wrappers. Returns the post-skip token list.
        while True:
            try:
                tokens = shlex.split(s, posix=True)
            except ValueError:
                return (s.split(), s)
            if not tokens:
                return (tokens, s)
            # Skip env-var assignments (FOO=bar) -- always.
            i = 0
            while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
                i += 1
            if i >= len(tokens):
                return (tokens, s)
            head = os.path.basename(tokens[i])
            if head == "cd" and i + 1 < len(tokens):
                # Look for `&&` / `;` after the directory
                rest_idx = i + 2
                if rest_idx < len(tokens) and tokens[rest_idx] in ("&&", ";"):
                    s = " ".join(_shell_quote(t) for t in tokens[rest_idx + 1:])
                    continue
            if head in SHELL_DASH_C and i + 1 < len(tokens) and tokens[i + 1] == "-c" and i + 2 < len(tokens):
                s = tokens[i + 2]
                continue
            if head in SKIP_PREFIXES and i + 1 < len(tokens):
                s = " ".join(_shell_quote(t) for t in tokens[i + 1:])
                continue
            # Reached a real command -- return tokens with env-vars stripped.
            return (tokens[i:], s)

    parts, _real = first_real_segment(cmd)
    if not parts:
        return "run"
    base = os.path.basename(parts[0])
    if "." in base:
        base = base.rsplit(".", 1)[0]
    if not base:
        return "run"
    # Include the second token if it looks like a subcommand / target
    # (`go test`, `make reinstall`, `npm run`, `cargo build`).
    if len(parts) >= 2:
        second = parts[1]
        if re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", second) and not second.startswith("-"):
            return f"{base}-{second}"
    return base


def _shell_quote(s: str) -> str:
    """Re-quote a shlex token for embedding back into a shell string."""
    if not s or re.search(r"[\s'\"\\$`&|;<>()*?{}\[\]!~#]", s):
        return "'" + s.replace("'", "'\"'\"'") + "'"
    return s


# -----------------------------------------------------------------------------
# Event parsing
# -----------------------------------------------------------------------------

@dataclass
class ParsedEvent:
    """A single parsed protocol event from stdout.log."""
    line_no: int
    at: int            # epoch seconds (file mtime fallback)
    kind: str          # section_start | section_end | event | metric | fail | malformed
    section: str | None  # implied or explicit
    payload: dict[str, Any] = field(default_factory=dict)


def parse_events(stdout_path: Path, parser_hint: str = "auto") -> list[ParsedEvent]:
    """Stream-parse stdout.log line by line, return all protocol events.

    Two parse modes coexist:
      1. Marker mode (always preferred): lines starting with `::run:: ` are
         parsed as the script's own protocol events. Manual instrumentation
         always wins.
      2. Adapter mode: when no markers appear in early output and an
         OutputAdapter recognizes the format (e.g. `go test`), synthetic
         events are generated from raw output. Section attribution and
         everything downstream (status, drill-down, grep) work unchanged.

    `parser_hint` controls adapter selection:
      - "auto" (default): if no markers appear in the first ADAPTER_SNIFF_LINES
        lines and an adapter sniffs successfully, that adapter is used.
      - "none": disable adapters; only marker mode.
      - "<adapter-name>" (e.g. "go-test"): force that adapter regardless of
        sniff result. Useful when output is interleaved with other text.
    """
    events: list[ParsedEvent] = []
    if not stdout_path.exists():
        return events

    current_section: str | None = None
    file_mtime = int(stdout_path.stat().st_mtime)

    # Phase 1: read all lines (sufficient for typical log sizes; same as
    # the prior implementation which also read line-by-line).
    with stdout_path.open("r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    # Decide whether to engage an adapter
    has_markers = any(l.startswith(WIRE_PREFIX) for l in all_lines[:ADAPTER_SNIFF_LINES])
    adapter: OutputAdapter | None = None
    if parser_hint != "none":
        if parser_hint == "auto":
            if not has_markers:
                adapter = _select_adapter([l.rstrip("\n") for l in all_lines[:ADAPTER_SNIFF_LINES]])
        elif parser_hint:
            adapter = _ADAPTERS_BY_NAME.get(parser_hint)
            if adapter is not None:
                adapter = adapter.__class__()  # fresh instance for stateful parsing

    # Phase 2: walk lines, dispatch to marker parser or adapter
    for line_no, line in enumerate(all_lines, start=1):
        if line.startswith(WIRE_PREFIX):
            payload_str = line[len(WIRE_PREFIX):].rstrip("\n")
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                events.append(ParsedEvent(
                    line_no=line_no,
                    at=file_mtime,
                    kind="malformed",
                    section=current_section,
                    payload={"raw": payload_str},
                ))
                continue

            verb = payload.get("v")
            if verb not in EVENT_VERBS:
                events.append(ParsedEvent(
                    line_no=line_no,
                    at=file_mtime,
                    kind="malformed",
                    section=current_section,
                    payload=payload,
                ))
                continue

            at = int(payload.get("ts") or file_mtime)
            section = payload.get("section") or current_section
            if verb == "section_start":
                section = payload.get("name") or section
                current_section = section
            elif verb == "section_end":
                section = payload.get("name") or section
                if current_section == section:
                    current_section = None

            events.append(ParsedEvent(
                line_no=line_no,
                at=at,
                kind=verb,
                section=section,
                payload=payload,
            ))
        elif adapter is not None:
            for ev in adapter.feed(line.rstrip("\n"), line_no, file_mtime):
                # Track section attribution for adapter-synthesized events too
                if ev.kind == "section_start":
                    current_section = ev.section
                elif ev.kind == "section_end":
                    if current_section == ev.section:
                        current_section = None
                events.append(ev)

    if adapter is not None:
        for ev in adapter.finalize(file_mtime):
            if ev.kind == "section_start":
                current_section = ev.section
            elif ev.kind == "section_end":
                if current_section == ev.section:
                    current_section = None
            events.append(ev)

    return events


# -----------------------------------------------------------------------------
# Output adapters: parse uninstrumented test output into synthetic events
# -----------------------------------------------------------------------------

# Lines of stdout to scan when deciding whether to engage an adapter
ADAPTER_SNIFF_LINES = 60


class OutputAdapter:
    """Base class for output adapters. Subclasses turn raw stdout lines into
    synthetic ParsedEvent streams so the rest of the runner (sections,
    status, drill-down, grep) works without instrumentation.

    Subclasses must:
      - set `name` to a stable identifier (used by parser_hint)
      - implement `sniff(early_lines)` returning bool
      - implement `feed(line, line_no, fallback_at)` returning a list of events
      - implement `finalize(fallback_at)` to close any still-open sections
    """
    name: str = "base"

    def sniff(self, early_lines: list[str]) -> bool:
        return False

    def feed(self, line: str, line_no: int, fallback_at: int) -> list[ParsedEvent]:
        return []

    def finalize(self, fallback_at: int) -> list[ParsedEvent]:
        return []


# go test output regexes
_GO_TEST_RUN_RE = re.compile(r"^=== RUN\s+(\S+)")
_GO_TEST_RESULT_RE = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)\s+\(([\d.]+)s\)")
_GO_TEST_PKG_OK_RE = re.compile(r"^ok\s+(\S+)\s+(?:\(cached\)|([\d.]+)s)")
_GO_TEST_PKG_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+(?:\[[^\]]+\]|([\d.]+)s)")
_GO_TEST_PKG_NOTEST_RE = re.compile(r"^\?\s+(\S+)\s+\[no test files\]")
_GO_TEST_PKG_BUILD_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")
_GO_TEST_FAIL_LINE_RE = re.compile(r"^\s*\S+\.go:\d+:")  # e.g. "    foo_test.go:42: assertion failed"


class GoTestAdapter(OutputAdapter):
    """Adapter for `go test` output.

    Synthesizes:
      - One section per package (named after the package import path).
      - Inside each section: per-test events (pass/fail/skip) + aggregated
        metrics (testsPass/testsFail/testsSkip/elapsedSec).
      - Failure detail: lines that look like `<file>.go:<lineno>:` between
        `--- FAIL: TestX` and the next package boundary are captured as
        events on the section so runner_section returns the assertion text.

    The "current package" is inferred lazily: go test interleaves output but
    each test result is followed by its package summary on its own line, so
    we buffer per-test results until we see the package line and then emit
    the section_start + section_end together.
    """
    name = "go-test"

    def __init__(self) -> None:
        # Buffered per-test results until a package line closes them
        self._tests: list[dict[str, Any]] = []
        # Per-test failure-detail capture. Go prints assertion lines BEFORE
        # the `--- FAIL: TestX` line, so we keep a rolling buffer keyed by
        # the test currently running (set by `=== RUN`) and attach it when
        # we see the FAIL marker.
        self._current_test: str | None = None
        self._pending_detail: list[tuple[int, str]] = []   # (line_no, text)
        # Once a FAIL is closed, detail lines are stored per-test for
        # emission when the package summary is reached.
        self._fail_details: dict[str, list[tuple[int, str]]] = {}

    def sniff(self, early_lines: list[str]) -> bool:
        for l in early_lines:
            if _GO_TEST_RUN_RE.match(l):
                return True
            if _GO_TEST_PKG_OK_RE.match(l) or _GO_TEST_PKG_FAIL_RE.match(l):
                return True
            if _GO_TEST_PKG_NOTEST_RE.match(l):
                return True
        return False

    def feed(self, line: str, line_no: int, fallback_at: int) -> list[ParsedEvent]:
        events: list[ParsedEvent] = []

        m = _GO_TEST_RUN_RE.match(line)
        if m:
            self._current_test = m.group(1)
            self._pending_detail = []
            return events

        m = _GO_TEST_RESULT_RE.match(line)
        if m:
            status_word, test_name, elapsed = m.group(1), m.group(2), float(m.group(3))
            status = {"PASS": "pass", "FAIL": "fail", "SKIP": "skip"}[status_word]
            self._tests.append({
                "name": test_name,
                "status": status,
                "elapsedSec": elapsed,
                "lineNo": line_no,
                "at": fallback_at,
            })
            if status == "fail" and self._pending_detail:
                self._fail_details[test_name] = list(self._pending_detail)
            self._pending_detail = []
            return events

        # Capture assertion lines (file.go:lineno: ...) BEFORE the result
        # marker. Belongs to the currently-running test.
        if self._current_test and _GO_TEST_FAIL_LINE_RE.match(line):
            self._pending_detail.append((line_no, line.strip()))
            return events

        # Build-failure form must be checked BEFORE the generic FAIL form
        # because both start with "FAIL <pkg>".
        m = _GO_TEST_PKG_BUILD_FAIL_RE.match(line)
        if m:
            pkg = m.group(1)
            events.extend(self._emit_package(pkg, "failed", 0.0, line_no, fallback_at, reason="build failed"))
            return events

        # Package summary lines close the current section
        m = _GO_TEST_PKG_OK_RE.match(line)
        if m:
            pkg = m.group(1)
            elapsed = float(m.group(2)) if m.group(2) else 0.0
            events.extend(self._emit_package(pkg, "ok", elapsed, line_no, fallback_at))
            return events

        m = _GO_TEST_PKG_FAIL_RE.match(line)
        if m:
            pkg = m.group(1)
            elapsed = float(m.group(2)) if m.group(2) else 0.0
            events.extend(self._emit_package(pkg, "failed", elapsed, line_no, fallback_at, reason="test failures"))
            return events

        m = _GO_TEST_PKG_NOTEST_RE.match(line)
        if m:
            pkg = m.group(1)
            events.extend(self._emit_package(pkg, "ok", 0.0, line_no, fallback_at, reason="no test files"))
            return events

        return events

    def finalize(self, fallback_at: int) -> list[ParsedEvent]:
        # If buffered tests exist with no package summary (e.g. go test crashed
        # mid-package), emit a synthetic package "unknown".
        if not self._tests:
            return []
        return self._emit_package("(incomplete)", "unknown", 0.0, 0, fallback_at, reason="no package summary")

    def _emit_package(self, pkg: str, status: str, elapsed: float, line_no: int,
                      fallback_at: int, reason: str | None = None) -> list[ParsedEvent]:
        out: list[ParsedEvent] = []
        # Use the line of the first test in the buffer for section_start; else
        # the package line itself.
        start_line = self._tests[0]["lineNo"] if self._tests else max(1, line_no - 1)
        start_at = self._tests[0]["at"] if self._tests else fallback_at

        out.append(ParsedEvent(
            line_no=start_line,
            at=start_at,
            kind="section_start",
            section=pkg,
            payload={"v": "section_start", "name": pkg, "synth": "go-test"},
        ))

        # Per-test events + tallies. For failing tests, attach captured
        # failure-detail lines so runner_section returns the assertion text.
        passed = failed = skipped = 0
        for t in self._tests:
            if t["status"] == "pass":
                passed += 1
            elif t["status"] == "fail":
                failed += 1
            elif t["status"] == "skip":
                skipped += 1
            msg = f"{t['status'].upper()} {t['name']} ({t['elapsedSec']}s)"
            out.append(ParsedEvent(
                line_no=t["lineNo"],
                at=t["at"],
                kind="event",
                section=pkg,
                payload={"v": "event", "msg": msg, "test": t["name"], "status": t["status"], "elapsedSec": t["elapsedSec"]},
            ))
            if t["status"] == "fail":
                for cap_line_no, text in self._fail_details.get(t["name"], []):
                    out.append(ParsedEvent(
                        line_no=cap_line_no,
                        at=fallback_at,
                        kind="event",
                        section=pkg,
                        payload={"v": "event", "msg": text, "kind": "failure-detail", "test": t["name"]},
                    ))

        # Aggregate metrics. Only include per-test counts when we actually
        # saw any per-test events; otherwise (typical without `go test -v`)
        # only the package elapsed time is known. The "no per-test data"
        # case is surfaced at the response level via testSummary, not here
        # in the metrics dict (which should stay numeric).
        metric_payload: dict[str, Any] = {
            "v": "metric",
            "section": pkg,
            "elapsedSec": elapsed,
        }
        if self._tests:
            metric_payload["testsPass"] = passed
            metric_payload["testsFail"] = failed
            metric_payload["testsSkip"] = skipped
        out.append(ParsedEvent(
            line_no=line_no or start_line,
            at=fallback_at,
            kind="metric",
            section=pkg,
            payload=metric_payload,
        ))

        end_payload: dict[str, Any] = {"v": "section_end", "name": pkg, "status": status}
        if reason:
            end_payload["reason"] = reason
        out.append(ParsedEvent(
            line_no=line_no or start_line,
            at=fallback_at,
            kind="section_end",
            section=pkg,
            payload=end_payload,
        ))

        # Reset state for next package
        self._tests = []
        self._current_test = None
        self._pending_detail = []
        self._fail_details = {}
        return out


# Registered adapters, ordered by sniff priority
_ADAPTERS: list[type[OutputAdapter]] = [GoTestAdapter]
_ADAPTERS_BY_NAME: dict[str, OutputAdapter] = {a().name: a() for a in _ADAPTERS}


def _select_adapter(early_lines: list[str]) -> OutputAdapter | None:
    """Run sniff() on each registered adapter; first match wins."""
    for cls in _ADAPTERS:
        inst = cls()
        if inst.sniff(early_lines):
            return inst
    return None


# -----------------------------------------------------------------------------
# Section model (derived from event stream)
# -----------------------------------------------------------------------------

@dataclass
class Section:
    name: str
    started_at: int
    ended_at: int | None = None
    status: str = "running"          # running | ok | failed | unknown
    exit_code: int | None = None
    reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    last_event_msg: str | None = None
    line_start: int = 0              # stdout.log line where section_start was emitted
    line_end: int | None = None      # stdout.log line where section_end was emitted

    @property
    def duration_sec(self) -> float:
        # Prefer the metrics-reported elapsedSec when available -- adapter
        # sections (e.g. go-test) report sub-second precision there, while
        # started_at/ended_at are integer wall-clock seconds. Without this,
        # any section that ran in <1s would report durationSec: 0.
        elapsed = self.metrics.get("elapsedSec") if isinstance(self.metrics, dict) else None
        if isinstance(elapsed, (int, float)):
            return float(elapsed)
        end = self.ended_at if self.ended_at is not None else int(time.time())
        return float(max(0, end - self.started_at))


def build_sections(events: list[ParsedEvent]) -> list[Section]:
    """Walk parsed events and build the ordered list of sections.

    Sections appear in the order they were started. A section without a
    matching section_end gets status="running" (if active) or "unknown"
    (if the run terminated).
    """
    sections: list[Section] = []
    by_name: dict[str, Section] = {}

    for ev in events:
        if ev.kind == "section_start":
            name = ev.payload.get("name") or ev.section
            if not name:
                continue
            sec = Section(
                name=name,
                started_at=ev.at,
                line_start=ev.line_no,
            )
            sections.append(sec)
            by_name[name] = sec

        elif ev.kind == "section_end":
            name = ev.payload.get("name") or ev.section
            if not name or name not in by_name:
                # Closing a section we never saw open: skip gracefully
                continue
            sec = by_name[name]
            sec.ended_at = ev.at
            status = ev.payload.get("status", "ok")
            if status not in SECTION_STATUSES:
                status = "ok"
            sec.status = status
            if "exit" in ev.payload:
                try:
                    sec.exit_code = int(ev.payload["exit"])
                except (TypeError, ValueError):
                    pass
            if "reason" in ev.payload:
                sec.reason = str(ev.payload["reason"])
            sec.line_end = ev.line_no

        elif ev.kind == "metric":
            name = ev.payload.get("section") or ev.section
            if name and name in by_name:
                # Merge all metric kv pairs (everything except v/section/ts)
                kvs = {k: v for k, v in ev.payload.items() if k not in ("v", "section", "ts")}
                by_name[name].metrics.update(kvs)

        elif ev.kind == "event":
            name = ev.payload.get("section") or ev.section
            if name and name in by_name:
                msg = ev.payload.get("msg", "")
                ts_str = time.strftime("%H:%M:%S", time.localtime(ev.at))
                entry: dict[str, Any] = {
                    "at": ev.at,
                    "ts": ts_str,
                    "msg": msg,
                    "lineNo": ev.line_no,
                }
                # Preserve adapter-tagged test status / failure-detail kind so
                # downstream consumers (cmd_section) can filter passing tests.
                test_name = ev.payload.get("test")
                if test_name:
                    entry["test"] = test_name
                test_status = ev.payload.get("status")
                if test_status:
                    entry["testStatus"] = test_status
                event_kind = ev.payload.get("kind")
                if event_kind:
                    entry["eventKind"] = event_kind
                by_name[name].events.append(entry)
                by_name[name].last_event_msg = msg

    return sections


# -----------------------------------------------------------------------------
# Run state synthesis
# -----------------------------------------------------------------------------

def _process_alive(pid: int) -> bool:
    """Return True if a process with this PID exists.

    Uses kill(pid, 0) which sends no signal but checks for existence.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it (shouldn't happen for runner)
        return True
    return True


def synthesize_run_state(meta: dict[str, Any], sections: list[Section]) -> dict[str, Any]:
    """Compute the live run state from meta + parsed sections.

    Reconciles:
      - meta says "running" but PID gone -> script crashed; finalize sections
      - meta says "exited" but a section is still "running" -> mark it "unknown"
      - meta says "exited" with exit 0 but failed sections -> result = "failed"
    """
    pid = meta.get("pid", 0) or 0
    state = meta.get("state", "running")
    exit_code = meta.get("exitCode")
    ended_at = meta.get("endedAt")

    # Detect crash: meta thinks running but PID is gone
    if state == "running" and pid > 0 and not _process_alive(pid):
        state = "exited"
        # If meta wasn't updated, exit code is unknown; signal -1
        if exit_code is None:
            exit_code = -1
        if ended_at is None:
            ended_at = int(time.time())

    # Reconcile sections: terminal run with open sections -> mark unknown
    if state == "exited":
        for sec in sections:
            if sec.status == "running":
                sec.status = "unknown"
                sec.ended_at = ended_at or int(time.time())

    # Compute result
    failed = [s for s in sections if s.status == "failed"]
    unknown = [s for s in sections if s.status == "unknown"]
    if state != "exited":
        result = None
    elif exit_code != 0 or failed or unknown:
        result = "failed"
    else:
        result = "success"

    return {
        "state": state,
        "exitCode": exit_code,
        "endedAt": ended_at,
        "result": result,
        "failed": failed,
        "unknown": unknown,
    }


# -----------------------------------------------------------------------------
# Tracker (per-agent delta cursor)
# -----------------------------------------------------------------------------

def _tracker_path(rdir: Path) -> Path:
    return rdir / FILE_TRACKER


def _load_tracker(rdir: Path) -> dict[str, Any]:
    return _read_json(_tracker_path(rdir), default={"agents": {}}) or {"agents": {}}


def _save_tracker(rdir: Path, tracker: dict[str, Any]) -> None:
    _atomic_write_json(_tracker_path(rdir), tracker)


def _agent_id_from_args(agent_arg: str | None) -> str:
    """Resolve an agent ID. Default: 'default'.

    MCP clients can pass `agent` to keep separate cursors when multiple
    agents poll the same run.
    """
    return agent_arg or "default"


# -----------------------------------------------------------------------------
# Status -- the primary tool
# -----------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    rdir = _resolve_run_dir(args)
    if rdir is None:
        return 1

    meta = _read_json(rdir / FILE_META, default={})
    if not meta:
        print(json.dumps({"error": f"run {args.run_id} has no meta.json"}), file=sys.stderr)
        return 1

    # Auto-wait protocol: if this run was started in blocking mode and is
    # still active, runner_status holds open the response for up to
    # BLOCKING_WAIT_SEC, returning when the run becomes terminal or the
    # window elapses. The agent's protocol then becomes "keep calling
    # runner_status until terminal" -- a single tool, repeated until done.
    #
    # Non-blocking runs (services) skip the wait by default so a status
    # check on a long-running dev server returns immediately. Either side
    # can override with --wait/--no-wait.
    wait_arg = getattr(args, "wait", None)
    is_blocking_run = bool(meta.get("blockingMode", False))
    if wait_arg is None:
        should_wait = is_blocking_run
    else:
        should_wait = bool(wait_arg)

    events: list[ParsedEvent] = []
    sections: list[Section] = []
    synth: dict[str, Any] = {}
    if should_wait:
        start = time.time()
        deadline = start + BLOCKING_WAIT_SEC
        while True:
            events = parse_events(rdir / FILE_STDOUT, parser_hint=meta.get("parser", "auto"))
            sections = build_sections(events)
            # Re-read meta in case the daemon finalized exitCode/state
            meta = _read_json(rdir / FILE_META, default=meta) or meta
            synth = synthesize_run_state(meta, sections)
            if synth["state"] == "exited":
                break
            if time.time() >= deadline:
                break
            time.sleep(_next_poll_interval(time.time() - start))
    else:
        events = parse_events(rdir / FILE_STDOUT, parser_hint=meta.get("parser", "auto"))
        sections = build_sections(events)
        synth = synthesize_run_state(meta, sections)

    # Persist final state if the synthesizer detected a crash and meta is stale
    if synth["state"] != meta.get("state") or synth.get("exitCode") != meta.get("exitCode"):
        meta["state"] = synth["state"]
        if synth["exitCode"] is not None:
            meta["exitCode"] = synth["exitCode"]
        if synth["endedAt"] is not None and meta.get("endedAt") is None:
            meta["endedAt"] = synth["endedAt"]
        _atomic_write_json(rdir / FILE_META, meta)

    # Tracker / delta computation
    agent_id = _agent_id_from_args(args.agent)
    tracker = _load_tracker(rdir)
    agent_state = tracker["agents"].get(agent_id, {
        "lastEventLine": 0,
        "lastStderrLine": 0,
        "lastCalledAt": None,
    })

    # If agent supplied an explicit `since` cursor, prefer that
    cursor_line = agent_state["lastEventLine"]
    if args.since is not None:
        cursor_line = max(0, args.since)

    new_events = [e for e in events if e.line_no > cursor_line]

    # Record updated cursor for next time
    new_cursor_line = events[-1].line_no if events else cursor_line
    agent_state["lastEventLine"] = new_cursor_line
    agent_state["lastCalledAt"] = int(time.time())
    # stderr line tracking
    stderr_total = _count_lines(rdir / FILE_STDERR)
    new_stderr_count = max(0, stderr_total - agent_state["lastStderrLine"])
    agent_state["lastStderrLine"] = stderr_total
    tracker["agents"][agent_id] = agent_state
    _save_tracker(rdir, tracker)

    # Build response per the agent-friendly compact shape
    response = _build_status_response(
        meta=meta,
        sections=sections,
        synth=synth,
        new_events=new_events,
        cursor_line=cursor_line,
        new_cursor_line=new_cursor_line,
        new_stderr_count=new_stderr_count,
        rdir=rdir,
        verbose=args.verbose,
    )

    # If we waited and the run is still active, attach the same
    # "stillRunning + followUp" fields the blocking start path uses --
    # the agent's protocol stays uniform across both entry points.
    if should_wait and synth["state"] != "exited":
        _attach_still_running(response, meta)

    # Optional embedded grep so the agent can ask "what's the status AND
    # show me lines matching X" in one round-trip.
    if getattr(args, "grep", None):
        response["grep"] = _run_grep(
            rdir,
            args.grep,
            stream=getattr(args, "grep_stream", None) or "both",
            a=int(getattr(args, "grep_a", 0) or 0),
            b=int(getattr(args, "grep_b", 0) or 0),
            limit=int(getattr(args, "grep_limit", 0) or 200),
            ignore_case=bool(getattr(args, "grep_ignore_case", False)),
        )

    print(json.dumps(response, indent=2 if args.pretty else None))
    return 0


def _count_lines(path: Path) -> int:
    """Cheap line counter -- byte-iterate, count newlines."""
    if not path.exists():
        return 0
    try:
        n = 0
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                n += chunk.count(b"\n")
        return n
    except OSError:
        return 0


def _read_last_lines(path: Path, n: int) -> list[str]:
    """Read the last n lines of a text file efficiently."""
    if not path.exists() or n <= 0:
        return []
    try:
        # Simple approach: read all, take last n. Cheap for typical log sizes.
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-n:]]
    except OSError:
        return []


def _read_all_lines(path: Path, hard_cap: int) -> list[str]:
    """Read up to `hard_cap` lines from a text file. Returns empty list if
    the file doesn't exist or can't be read. Bounded so a runaway log
    can't load gigabytes into memory.
    """
    if not path.exists() or hard_cap <= 0:
        return []
    try:
        out: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= hard_cap:
                    break
                out.append(line.rstrip("\n"))
        return out
    except OSError:
        return []


def _number_lines(lines: list[str], start: int) -> list[str]:
    """Prefix each line with its 1-based file line number, padded so the
    column of the actual text stays visually aligned. The format is
    `   42: <text>` -- the agent can read the number to know exactly
    which file line it's looking at, and gaps in numbering make
    omitted regions self-evident.
    """
    if not lines:
        return []
    last_no = start + len(lines) - 1
    width = max(4, len(str(last_no)))
    return [f"{start + i:>{width}}: {line}" for i, line in enumerate(lines)]


def _build_status_response(
    *,
    meta: dict[str, Any],
    sections: list[Section],
    synth: dict[str, Any],
    new_events: list[ParsedEvent],
    cursor_line: int,
    new_cursor_line: int,
    new_stderr_count: int,
    rdir: Path,
    verbose: bool,
) -> dict[str, Any]:
    """Construct the agent-friendly status response (Buddy-trimmed shape)."""
    # For agent-conversation runs (research_and_code_assistant_agent), lazily
    # extract the backend sessionId from stderr.log NDJSON and persist it to
    # meta.json on first capture. No-op for regular runs.
    meta = _maybe_capture_agent_session(rdir, meta)
    state = synth["state"]
    result = synth["result"]
    exit_code = synth["exitCode"]
    terminal = state == "exited"
    started_at = meta.get("startedAt", int(time.time()))
    ended_at = meta.get("endedAt")
    duration_sec = (ended_at if ended_at is not None else int(time.time())) - started_at

    # Identify failed/unknown/active
    failed_names = [s.name for s in sections if s.status == "failed"]
    unknown_names = [s.name for s in sections if s.status == "unknown"]
    active = next((s for s in sections if s.status == "running"), None)
    sections_done = sum(1 for s in sections if s.status in ("ok", "failed", "unknown"))

    # Last event time + stall calculation
    last_event_at = max((e.at for e in parse_events(rdir / FILE_STDOUT)), default=started_at)
    stalled_for_sec = max(0, int(time.time()) - last_event_at) if not terminal else 0

    # active section snapshot (one section, compact)
    active_state: dict[str, Any] | None = None
    if active is not None:
        active_state = {
            "name": active.name,
            "status": active.status,
            "durationSec": active.duration_sec,
        }
        if active.metrics:
            active_state["metrics"] = active.metrics
        if active.last_event_msg:
            active_state["lastEvent"] = active.last_event_msg

    # recently changed sections: those whose section_end appeared after cursor
    recently_changed = []
    for sec in sections:
        if sec.line_end is not None and sec.line_end > cursor_line:
            entry = {
                "name": sec.name,
                "status": sec.status,
                "durationSec": sec.duration_sec,
            }
            if sec.exit_code is not None:
                entry["exitCode"] = sec.exit_code
            if sec.reason:
                entry["reason"] = sec.reason
            if sec.metrics:
                entry["metrics"] = sec.metrics
            recently_changed.append(entry)

    # delta.newEvents: tagged with kind+section+at+msg.
    #
    # CRITICAL: adapter-driven runs (e.g. go test with hundreds of tests) can
    # produce one event per test. We compact this for the agent:
    #
    #   - In non-verbose mode, suppress passing/skipping per-test events.
    #     Keep failing tests, failure-detail lines, section boundaries, and
    #     metrics. The full per-test detail is available via runner_section.
    #   - Aggregate suppressed counts into delta.suppressedTestEvents so the
    #     agent knows what was hidden (and can ask for verbose if needed).
    #   - Cap the surviving event list so even a non-adapter pathological
    #     run can't blow out the agent's context.
    DELTA_EVENT_CAP = 200 if not verbose else 2000
    delta_events: list[dict[str, Any]] = []
    suppressed_pass = suppressed_skip = 0
    truncated_events = 0
    for ev in new_events:
        # Filter passing/skipping test events from adapters when not verbose
        if not verbose and ev.kind == "event" and ev.payload.get("status") in ("pass", "skip") and ev.payload.get("test"):
            if ev.payload.get("status") == "pass":
                suppressed_pass += 1
            else:
                suppressed_skip += 1
            continue
        entry: dict[str, Any] = {
            "kind": ev.kind,
            "at": ev.at,
        }
        if ev.section:
            entry["section"] = ev.section
        if ev.kind == "event":
            entry["msg"] = ev.payload.get("msg", "")
            # Tag failing test events so agents can spot them at a glance
            if ev.payload.get("status") == "fail" and ev.payload.get("test"):
                entry["test"] = ev.payload["test"]
                entry["status"] = "fail"
        elif ev.kind == "section_end":
            if "exit" in ev.payload:
                entry["exit"] = ev.payload["exit"]
            if "status" in ev.payload:
                entry["status"] = ev.payload["status"]
        elif ev.kind == "metric":
            entry["kv"] = {k: v for k, v in ev.payload.items() if k not in ("v", "section", "ts")}
        elif ev.kind == "fail":
            entry["msg"] = ev.payload.get("msg", "")
        if verbose:
            entry["lineNo"] = ev.line_no
            entry["raw"] = ev.payload
        if len(delta_events) >= DELTA_EVENT_CAP:
            truncated_events += 1
            continue
        delta_events.append(entry)

    # stderr sample only on terminal failure (Buddy MVP rule)
    stderr_sample: list[str] | None = None
    # stderr_sample is now superseded by the line-numbered stderr block
    # built later (see "stderr surfacing" below). Kept as None here so the
    # downstream `if stderr_sample:` guard becomes a no-op; remove this
    # local + that guard once nothing else references the old shape.
    stderr_sample = None

    # fatalMsg: surface from any "fail" event
    fatal_msg: str | None = None
    for ev in parse_events(rdir / FILE_STDOUT):
        if ev.kind == "fail":
            fatal_msg = ev.payload.get("msg")
            break

    # The response, decision-fields-first. Fields that are conditionally
    # interesting (active/failed sections, fatal messages, stale signals)
    # are added below only when they carry information -- so a clean
    # success response stays compact and a failure response highlights
    # exactly the keys the agent should look at.
    response: dict[str, Any] = {
        "runId": meta["runId"],
        "name": meta.get("name", "run"),
        "pid": meta.get("pid"),
        "startedAt": meta.get("startedAt"),
        "state": state,
        "terminal": terminal,
        "result": result,
        "exitCode": exit_code,

        "durationSec": duration_sec,
        "lastEventAt": last_event_at,

        "sectionsDone": sections_done,
        "sectionsFailed": len(failed_names),

        "recentlyChangedSections": recently_changed,

        "delta": {
            "cursor": str(new_cursor_line),
            "since": str(cursor_line),
            "newEvents": delta_events,
            "newEventCount": len(delta_events),
            **({"suppressedTestEvents": {
                "passed": suppressed_pass,
                "skipped": suppressed_skip,
                "hint": "Per-test pass/skip events hidden to keep response compact. Pass verbose:true to see them, or call runner_section to drill into a package.",
            }} if (suppressed_pass + suppressed_skip) > 0 else {}),
            **({"truncatedEvents": truncated_events,
                "truncatedHint": f"newEvents capped at {DELTA_EVENT_CAP}; {truncated_events} more were dropped. Use since=<cursor> to page, or call runner_section / runner_grep for specific detail."}
               if truncated_events > 0 else {}),
        },

    }
    # Conditionally-emitted fields. Each is added only when it's actually
    # informative for the agent. Keeps clean-success responses compact.
    if fatal_msg:
        response["fatalMsg"] = fatal_msg
    if stalled_for_sec > 0:
        response["stalledForSec"] = stalled_for_sec
    if active is not None:
        response["activeSection"] = active.name
        if active_state is not None:
            response["activeSectionState"] = active_state
    if failed_names:
        response["failedSections"] = failed_names
    if unknown_names:
        response["unknownSections"] = unknown_names
    # stderrCount: always emitted (it's useful even when 0 -- "no stderr"
    # is itself a signal). stderrNewCount: only when nonzero (delta).
    stderr_total = _count_lines(rdir / FILE_STDERR)
    response["stderrCount"] = stderr_total
    if new_stderr_count > 0 and new_stderr_count != stderr_total:
        response["stderrNewCount"] = new_stderr_count
    if stderr_sample:
        # Legacy path -- preserved for transition, currently always None.
        response["stderrSample"] = stderr_sample
    # pollAfterSec is omitted entirely when terminal -- no point recommending
    # a poll cadence for a finished run.
    poll_after = _suggest_poll_after(state, stalled_for_sec, len(new_events))
    if poll_after is not None:
        response["pollAfterSec"] = poll_after
    # runRoot is verbose-only -- agents should never need to read raw logs;
    # the structured tools cover everything. Available via verbose for debug.
    if verbose:
        response["runRoot"] = str(rdir)

    # Surface adapter info so agents know whether structure came from manual
    # markers or auto-detected output (e.g. go test). Look at any synthetic
    # marker in section_start payloads.
    parser_used: str | None = None
    for sec in sections:
        # Section payload isn't carried into the Section dataclass, so check
        # the first event we attributed: if any was kind=section_start with
        # synth in its payload, that's the adapter name. Cheap second pass:
        for ev in [e for e in parse_events(rdir / FILE_STDOUT, parser_hint=meta.get("parser", "auto")) if e.kind == "section_start" and e.section == sec.name]:
            synth_val = ev.payload.get("synth")
            if synth_val:
                parser_used = str(synth_val)
                break
        if parser_used:
            break
    if parser_used:
        response["parserUsed"] = parser_used
        # Aggregate per-test counts across all sections so the agent sees
        # totals at a glance without scanning every section's metrics.
        total_pass = total_fail = total_skip = 0
        per_test_known = False
        failed_tests: list[dict[str, str]] = []
        for sec in sections:
            m = sec.metrics or {}
            if "testsPass" in m or "testsFail" in m or "testsSkip" in m:
                per_test_known = True
                total_pass += int(m.get("testsPass", 0) or 0)
                total_fail += int(m.get("testsFail", 0) or 0)
                total_skip += int(m.get("testsSkip", 0) or 0)
            # Collect failing test names from section events for surfacing
            for sev in sec.events:
                msg = sev.get("msg", "")
                if msg.startswith("FAIL "):
                    test_name = msg.split(" ", 2)[1] if len(msg.split()) >= 2 else msg
                    failed_tests.append({"package": sec.name, "test": test_name})
        summary: dict[str, Any] = {
            "packagesRun": len(sections),
            "packagesFailed": len(failed_names),
        }
        if per_test_known:
            summary["testsPass"] = total_pass
            summary["testsFail"] = total_fail
            summary["testsSkip"] = total_skip
            if failed_tests:
                summary["failedTests"] = failed_tests[:50]  # cap; full list via runner_section
                if len(failed_tests) > 50:
                    summary["failedTestsTruncated"] = len(failed_tests)
        else:
            # Per-test counts unavailable (no `-v`). This is informational --
            # it does NOT mean nothing ran. The packages-level pass/fail is
            # still authoritative; check status / packagesRun / packagesFailed
            # to know what happened.
            summary["perTestCountsAvailable"] = False
            summary["perTestCountsHint"] = "Per-test counts unavailable (no -v). Package-level pass/fail is authoritative; see status / packagesRun / packagesFailed."
        # Structured next-step actions: machine-readable, beats parsing
        # English. The agent can dispatch these directly.
        next_calls: list[dict[str, Any]] = []
        if failed_tests:
            seen_pkgs: set[str] = set()
            for ft in failed_tests:
                pkg = ft["package"]
                if pkg in seen_pkgs:
                    continue
                seen_pkgs.add(pkg)
                next_calls.append({
                    "tool": "runner_section",
                    "args": {"runId": meta["runId"], "name": pkg},
                    "purpose": f"Inspect failed package {pkg}: failed tests + assertion lines.",
                })
        elif failed_names:
            for pkg in failed_names:
                next_calls.append({
                    "tool": "runner_section",
                    "args": {"runId": meta["runId"], "name": pkg},
                    "purpose": f"Inspect failed section {pkg}.",
                })
        # Status: distinguish "tests failed" from "all green with per-test
        # data" from "packages passed but no per-test breakdown" (typical of
        # `go test ./...` without -v) from "literally no packages ran".
        # Lumping the last three together caused agents to misread a clean
        # multi-package run as "no tests ran".
        if next_calls:
            summary["nextCalls"] = next_calls
            summary["status"] = "failed"
        elif per_test_known and total_pass > 0:
            summary["status"] = "all_passed"
        elif len(sections) > 0 and len(failed_names) == 0:
            # Packages were tested and all returned ok, we just don't have
            # per-test counts (no -v). This IS a successful test run.
            summary["status"] = "packages_ok"
        else:
            summary["status"] = "no_tests"
        response["testSummary"] = summary

    # Sub-agent conversation summary -- present only for runs spawned via
    # research_and_code_assistant_agent. The caller never has to look at
    # the backend sessionId; it just keeps using runId across turns.
    # This block exists so the agent can confirm which backend, what the
    # last reason was (stop / tool-calls / etc.), and how many turns deep
    # the conversation is at a glance.
    agent_summary = meta.get("_agentSummary")
    if agent_summary:
        response["agent"] = {
            "runtime": agent_summary["runtime"],
            "turn": agent_summary.get("turnCount", 1),
        }
        if agent_summary.get("sessionId"):
            response["agent"]["backendSessionId"] = agent_summary["sessionId"]
        if agent_summary.get("lastTokens") is not None:
            response["agent"]["lastTokens"] = agent_summary["lastTokens"]
        if agent_summary.get("lastReason"):
            response["agent"]["lastReason"] = agent_summary["lastReason"]
        if agent_summary.get("toolCallCount"):
            response["agent"]["toolCallCount"] = agent_summary["toolCallCount"]

    # Compact agent-mode view: when a sub-agent backend rendered this run,
    # replace the normal verbose stdout/stderr dump with a per-backend
    # synthesized compact view. The verbose transcript is still on disk
    # (stdout.log) for runner_grep / runner_section drill-down -- we just
    # stop pushing it through the response by default because a typical
    # multi-tool turn produces ~700 lines of rendered transcript that
    # eats the caller's context window for no good reason. The compact
    # view: each text reply in full, plus one-line summaries per tool
    # call ("[tool: bash] description -> 142 lines (ok)").
    #
    # When the turn is still running, the compact view also surfaces a
    # currentActivity block (most recent tool + status + tokens + duration)
    # so the caller knows what the sub-agent is doing without dumping
    # partial transcript bytes.
    #
    # Each agent backend implements its own compact_view() because event
    # shapes are backend-specific (opencode NDJSON looks nothing like
    # what claude or other backends produce).
    agent_compact: dict[str, Any] | None = None
    if meta.get("agentRuntime"):
        agents = _agents_module()
        if agents is not None:
            runtime = agents.get(meta["agentRuntime"]) if hasattr(agents, "get") else None
            if runtime is not None and hasattr(runtime, "compact_view"):
                try:
                    agent_compact = runtime.compact_view(
                        rdir,
                        terminal=terminal,
                        started_at=meta.get("startedAt"),
                    )
                except Exception:
                    # compact_view failures must not break status. Fall
                    # back to surfacing nothing agent-specific; the
                    # standard fields above still describe the run.
                    agent_compact = None
        if agent_compact:
            # Refresh the agent block's progress fields with the freshly
            # computed values (the extract() summary above is good for
            # cross-call persistence but compact_view sees the latest
            # state on each call).
            if "agent" not in response:
                response["agent"] = {"runtime": meta["agentRuntime"]}
            if agent_compact.get("tokensSoFar") is not None:
                response["agent"]["tokensSoFar"] = agent_compact["tokensSoFar"]
            if agent_compact.get("toolCallCount"):
                response["agent"]["toolCallCount"] = agent_compact["toolCallCount"]
            if agent_compact.get("turnDurationSec") is not None:
                response["agent"]["turnDurationSec"] = agent_compact["turnDurationSec"]

            # TERMINAL: surface the focused final reply at the top level.
            # This is what the caller actually wants -- the sub-agent's
            # answer in full, NOT a tool-by-tool replay. The full rendered
            # transcript stays on disk for runner_grep / runner_section.
            #
            # IN-FLIGHT: surface the progress block (currentActivity +
            # recentToolCalls) so the caller can see what's happening
            # without streaming the whole thing.
            fr = agent_compact.get("finalReply")
            if fr:
                response["finalReply"] = {
                    "text": fr.get("text", ""),
                    "totalToolCalls": fr.get("totalToolCalls", 0),
                }
                if fr.get("totalTokens") is not None:
                    response["finalReply"]["totalTokens"] = fr["totalTokens"]
                if fr.get("recentToolCalls"):
                    response["finalReply"]["recentToolCalls"] = fr["recentToolCalls"]
                if fr.get("backendErrors"):
                    response["finalReply"]["backendErrors"] = fr["backendErrors"]
                # Note where the full transcript lives. Single hint, not
                # the per-call hint storm that lived in the old design.
                response["transcriptHint"] = (
                    "finalReply.text is the sub-agent's complete answer. "
                    "Full rendered transcript with every tool input/output "
                    "is on disk at stdout.log -- use runner_grep or "
                    "runner_section for specific detail."
                )

            pg = agent_compact.get("progress")
            if pg:
                if pg.get("currentActivity"):
                    response["agent"]["currentActivity"] = pg["currentActivity"]
                if pg.get("recentToolCalls"):
                    response["agent"]["recentToolCalls"] = pg["recentToolCalls"]
                if pg.get("backendErrors"):
                    response["agent"]["backendErrors"] = pg["backendErrors"]
                if pg.get("lastReason"):
                    response["agent"]["lastReason"] = pg["lastReason"]

            # INTERRUPTED: the wrapping process exited but the turn never
            # reached reason=stop, and the NDJSON has a backend error event
            # (rate limit, API outage, etc.). Surface it explicitly so the
            # caller doesn't see "terminal but no reply" and poll-bomb.
            # The hint tells them to dispatch 'continue' on the same runId
            # -- opencode preserves session state across LLM errors so the
            # sub-agent picks up where it left off.
            interrupted = agent_compact.get("interrupted")
            if interrupted:
                response["agent"]["interrupted"] = True
                response["agent"]["interruptReason"] = interrupted.get("reason", "")
                if interrupted.get("code"):
                    response["agent"]["interruptCode"] = interrupted["code"]
                if interrupted.get("kind"):
                    response["agent"]["interruptKind"] = interrupted["kind"]
                response["followUp"] = (
                    f"Sub-agent turn was interrupted by a backend error "
                    f"({interrupted.get('reason')!r}, code={interrupted.get('code')!r}). "
                    f"The conversation state is preserved. To resume, dispatch "
                    f"another turn on the SAME runId with ask='continue' (or "
                    f"any follow-up prompt) -- the sub-agent will pick up "
                    f"where it left off. Do NOT poll for this turn's reply; "
                    f"there isn't one."
                )
                # Demote the surface result so the caller's checks for
                # `result === "success"` don't fire on an incomplete turn.
                response["result"] = "interrupted"

    # Surface the agent's own description back so they can remember why
    # they started this run. Only emitted when set -- keeps responses
    # compact for runs without one.
    if meta.get("description"):
        response["description"] = meta["description"]

    # Surface restart history so the agent can see if a service has been
    # cycled. Only when nonzero so quiet runs stay compact.
    if meta.get("restartCount"):
        response["restartCount"] = meta["restartCount"]
        if meta.get("previousEndedAt"):
            response["previousRunEndedAt"] = meta["previousEndedAt"]

    # Detected endpoints (URLs / ports). Lets services advertise themselves
    # without the agent having to grep.
    endpoints = _detect_endpoints(rdir)
    if endpoints:
        response["endpoints"] = endpoints

    # stdout surfacing: for non-adapter terminal runs (plain build/make
    # commands, scripts without runnerlog markers), give the agent the
    # actual output -- not just a trailing tail. Agents commonly stuff
    # multi-step cmds (e.g. `cd && cmd && tail logs`) where the operation's
    # own output sits in the MIDDLE of the captured stdout, so a tail-only
    # view often misses the part the agent cares about.
    #
    # Sizing strategy (output is always line-numbered as `<lineNo>: <text>`
    # so the agent can see exactly what it has and where the gaps are):
    #   - Small output (<= STDOUT_FULL_LINES): include the entire stdout
    #     as `stdout`, line-numbered. The agent sees everything.
    #   - Larger output: include `stdoutHead` + a literal `... <N lines
    #     omitted> ...` marker line + `stdoutTail`, all line-numbered.
    #     The omission gap is unmistakable. Use runner_grep or
    #     runner_status `since: <lineNo>` to fetch arbitrary windows.
    #
    # Adapter-driven runs (e.g. go-test) already surface structured
    # decision data via testSummary / failedSections / etc., so the raw
    # stdout is omitted from the response unless verbose is set.
    #
    # Agent-mode runs are also skipped here: the compact agent view above
    # already supplies `transcript` (text + 1-line tool summaries) which
    # replaces the multi-hundred-line rendered stdout for context safety.
    # The full transcript lives on disk for runner_grep / runner_section.
    is_agent_run = bool(meta.get("agentRuntime"))
    if terminal and not parser_used and not is_agent_run:
        all_stdout = _read_all_lines(rdir / FILE_STDOUT, STDOUT_MAX_LINES_HARD_CAP)
        if all_stdout:
            total = len(all_stdout)
            response["stdoutTotalLines"] = total
            if total <= STDOUT_FULL_LINES:
                response["stdout"] = _number_lines(all_stdout, 1)
            else:
                head_count = STDOUT_HEAD_LINES
                tail_count = STDOUT_TAIL_LINES
                head_lines = all_stdout[:head_count]
                tail_lines = all_stdout[-tail_count:]
                omitted = total - head_count - tail_count
                tail_start_lineno = total - tail_count + 1
                # Single combined `stdout` block with a clear gap marker so
                # the agent never confuses a head+tail view for a continuous
                # one. Each line is prefixed with its file lineNo.
                response["stdout"] = (
                    _number_lines(head_lines, 1)
                    + [f"... <{omitted} lines omitted; lines {head_count + 1}..{tail_start_lineno - 1}> ..."]
                    + _number_lines(tail_lines, tail_start_lineno)
                )
                response["stdoutOmittedLines"] = omitted
                response["stdoutOmittedRange"] = {
                    "fromLine": head_count + 1,
                    "toLine": tail_start_lineno - 1,
                }
                response["stdoutHint"] = (
                    f"Output too large for full inclusion ({total} lines). "
                    f"Lines 1..{head_count} and {tail_start_lineno}..{total} are shown above; "
                    f"the {omitted}-line middle was elided. Use runner_grep for specific patterns "
                    "or runner_status with `since: <lineNo>` to see arbitrary windows."
                )

    # stderr surfacing: same model as stdout. Surface FULL content when small,
    # otherwise line-numbered head+tail with an explicit gap marker. Applied
    # on every terminal run (not just failures) because tools commonly write
    # warnings/diagnostics to stderr even on success.
    #
    # Agent-mode skip: stderr.log for sub-agent runs is mostly raw NDJSON
    # (sometimes ~70KB+) that the caller doesn't want dumped into context.
    # If the backend wrote any real error text (e.g. "Session not found"),
    # the compact view above already captured it into the transcript.
    if terminal and not is_agent_run:
        all_stderr = _read_all_lines(rdir / FILE_STDERR, STDOUT_MAX_LINES_HARD_CAP)
        if all_stderr:
            stot = len(all_stderr)
            if stot <= STDOUT_FULL_LINES:
                response["stderr"] = _number_lines(all_stderr, 1)
            else:
                hc = STDOUT_HEAD_LINES
                tc = STDOUT_TAIL_LINES
                head_lines = all_stderr[:hc]
                tail_lines = all_stderr[-tc:]
                omitted = stot - hc - tc
                tail_start_lineno = stot - tc + 1
                response["stderr"] = (
                    _number_lines(head_lines, 1)
                    + [f"... <{omitted} lines omitted; lines {hc + 1}..{tail_start_lineno - 1}> ..."]
                    + _number_lines(tail_lines, tail_start_lineno)
                )
                response["stderrOmittedLines"] = omitted
                response["stderrOmittedRange"] = {
                    "fromLine": hc + 1,
                    "toLine": tail_start_lineno - 1,
                }
                response["stderrHint"] = (
                    f"stderr too large for full inclusion ({stot} lines). "
                    f"Lines 1..{hc} and {tail_start_lineno}..{stot} are shown above; "
                    f"the {omitted}-line middle was elided. Use runner_grep "
                    f"`stream: stderr` for specific patterns."
                )

    # Warning scan: even when exitCode == 0, output may contain ERROR/FAIL
    # lines from sub-processes (a common pattern in shell scripts that
    # tolerate partial failures). Surface a warning count + sample so the
    # agent doesn't trust a green result when stdout/stderr are screaming.
    # Cheap O(n) scan; bounded to terminal runs only.
    #
    # Agent-mode skip: a sub-agent's tool outputs (`go test`, grep, etc.)
    # routinely contain words like FAIL and panic as data. Scanning them
    # for "warnings" produces noise without signal.
    if terminal and not is_agent_run:
        warnings = _scan_for_warnings(rdir)
        if warnings:
            response["warnings"] = warnings

    if verbose:
        response["sections"] = [_section_to_dict(s, verbose=True) for s in sections]
        response["meta"] = meta

    return response


# Stdout sizing thresholds for non-adapter terminal responses.
#
# STDOUT_FULL_LINES: include the entire stdout when it fits under this
#   line count. The whole point: agents put their actual operational
#   output anywhere in the cmd (start, middle, after a `&& tail logs`
#   suffix), and a trailing tail-only view often misses the part the
#   agent cares about. For typical build/test/install runs this fits
#   comfortably.
# STDOUT_HEAD_LINES + STDOUT_TAIL_LINES: when output is too large,
#   surface both the head (the cmd's prelude / setup output) and the
#   tail (final status / errors). The middle is reachable via
#   runner_grep, runner_section, or runner_status with `since`.
# STDOUT_MAX_LINES_HARD_CAP: bound _read_all_lines so a runaway log
#   can't load gigabytes into memory; lines beyond this aren't read.
STDOUT_FULL_LINES = 400
STDOUT_HEAD_LINES = 80
STDOUT_TAIL_LINES = 80
STDOUT_MAX_LINES_HARD_CAP = 50000

# Patterns that often signal a problem even when exit code is 0. Tight
# regex -- we want low false-positive rate. The scan runs on terminal-run
# stdout+stderr and surfaces a count + sample if anything matches.
_WARNING_REGEXES = [
    re.compile(r"^\s*ERROR\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*error:", re.MULTILINE),
    re.compile(r"^\s*FAIL\b"),
    re.compile(r"^\s*FAILED\b"),
    re.compile(r"^\s*FATAL\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*panic:"),
    re.compile(r"^\s*Traceback "),
    re.compile(r"connection refused", re.IGNORECASE),
]
_WARNING_SAMPLE = 5      # surface up to this many matching lines verbatim
_WARNING_SCAN_BYTES = 1024 * 1024   # cap stdout/stderr scan to 1 MB each


def _scan_for_warnings(rdir: Path) -> dict[str, Any] | None:
    """Scan stdout + stderr for error-shaped lines.

    Returns None if nothing concerning was found; otherwise a dict with
    `count`, `sample` (up to _WARNING_SAMPLE matching lines, with stream
    + lineNo), and a `hint` directing the agent to runner_grep for the
    full picture.

    The point: even when exit code is 0, an agent should not blindly
    trust `result: "success"` if a script tolerated failures. Many real
    builds (e.g. `make reinstall` with optional seeding steps) report
    success at the make level while individual steps logged ERRORs.
    """
    found: list[dict[str, Any]] = []
    total = 0
    for stream_name, path in (("stdout", rdir / FILE_STDOUT), ("stderr", rdir / FILE_STDERR)):
        if not path.exists():
            continue
        try:
            size = path.stat().st_size
            with path.open("r", encoding="utf-8", errors="replace") as f:
                if size > _WARNING_SCAN_BYTES:
                    f.seek(size - _WARNING_SCAN_BYTES)
                    f.readline()  # skip partial line
                for i, line in enumerate(f, start=1):
                    line = line.rstrip("\n")
                    for rx in _WARNING_REGEXES:
                        if rx.search(line):
                            total += 1
                            if len(found) < _WARNING_SAMPLE:
                                found.append({
                                    "stream": stream_name,
                                    "lineNo": i,
                                    "line": line[:200],   # cap each line
                                })
                            break
        except OSError:
            continue
    if not found:
        return None
    return {
        "count": total,
        "sample": found,
        "hint": (
            "Output contains ERROR / FAIL / panic / fatal lines even though "
            "exit code may be 0. Use runner_grep with the same patterns or "
            "inspect stdoutTail / runRoot to confirm the run actually did "
            "what you wanted."
        ),
    }


def _maybe_capture_agent_session(rdir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Lazy extractor for agent-conversation runs.

    For any run whose meta has agentRuntime set, dispatches to the matching
    helper's extract() to scan stderr.log NDJSON and pull out the backend
    session id + summary stats. The very first time we successfully extract
    a sessionId, it's written back into meta.json so subsequent turns can
    resume the conversation without re-scanning.

    Returns the (possibly-updated) meta. Always safe to call; for non-agent
    runs it's a no-op O(1) dict check.
    """
    runtime_name = meta.get("agentRuntime")
    if not runtime_name:
        return meta
    agents = _agents_module()
    if agents is None:
        return meta
    runtime = agents.get(runtime_name) if hasattr(agents, "get") else None
    if runtime is None:
        return meta
    try:
        info = runtime.extract(rdir) or {}
    except Exception:
        # Extractor failures should never break status. Surface nothing.
        info = {}
    # Always remember the latest summary so cmd_list / status can show it.
    # Persist sessionId on first capture so future turns can use -s without
    # re-reading stderr.log; the rest of `info` is small and re-derived
    # each call (tokens / reason / tool count drift turn-to-turn).
    sid = info.get("sessionId")
    if sid and not meta.get("agentSessionId"):
        meta = dict(meta)
        meta["agentSessionId"] = sid
        _atomic_write_json(rdir / FILE_META, meta)
    # Stash the rest under a transient key the response builder can read
    # without re-parsing. NOT persisted -- recomputed each read.
    meta_with_summary = dict(meta)
    meta_with_summary["_agentSummary"] = {
        "runtime": runtime_name,
        "sessionId": meta.get("agentSessionId") or sid,
        "lastTokens": info.get("lastTokens"),
        "lastReason": info.get("lastReason"),
        "toolCallCount": info.get("toolCallCount", 0),
        "turnCount": meta.get("agentTurnCount", 1),
    }
    return meta_with_summary


def _attach_still_running(response: dict[str, Any], meta: dict[str, Any]) -> None:
    """Attach the standard 'still running, just call X again' fields to
    a status response. Used by both the runner_start blocking return
    path and runner_status's own auto-wait path so the agent sees the
    same protocol regardless of which tool surfaced it.

    The followUp message points at the right poll tool: sub-agent runs
    poll via research_and_code_assistant_agent (with just runId, no
    ask); everything else polls via runner_status.
    """
    response["stillRunning"] = True
    response["blockingWaitSec"] = BLOCKING_WAIT_SEC
    run_id = meta.get("runId")
    if meta.get("agentRuntime"):
        response["followUp"] = (
            f"The {BLOCKING_WAIT_SEC}s wait window elapsed; the sub-agent "
            f"turn is STILL ACTIVE (NOT killed). Call "
            f"research_and_code_assistant_agent with just runId={run_id!r} "
            f"(no ask) to keep waiting for the response -- it auto-waits "
            f"another {BLOCKING_WAIT_SEC}s and returns finalReply when "
            f"ready. Repeat until terminal:true. Use runner_kill to abort."
        )
    else:
        response["followUp"] = (
            f"The {BLOCKING_WAIT_SEC}s wait window elapsed; the run is STILL ACTIVE "
            f"(NOT killed). Just call runner_status with runId={run_id!r} "
            f"again -- it will automatically wait another {BLOCKING_WAIT_SEC}s for "
            f"this blocking run. Repeat until terminal:true. Use runner_kill to abort."
        )


def _suggest_poll_after(state: str, stalled_for_sec: int, new_event_count: int) -> int | None:
    """Heuristic poll cadence:
       - terminal: None (no point polling; field is omitted from response)
       - active + busy: 5s
       - active + quiet: 15s
       - stalled long: 30s (something's wrong; let agent decide to investigate)
    """
    if state == "exited":
        return None
    if stalled_for_sec > 60:
        return 30
    if new_event_count > 5:
        return 5
    return DEFAULT_POLL_SEC


def _section_to_dict(sec: Section, *, verbose: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": sec.name,
        "status": sec.status,
        "durationSec": sec.duration_sec,
        "startedAt": sec.started_at,
        "endedAt": sec.ended_at,
    }
    if sec.exit_code is not None:
        d["exitCode"] = sec.exit_code
    if sec.reason:
        d["reason"] = sec.reason
    if sec.metrics:
        d["metrics"] = sec.metrics
    if verbose:
        d["lineStart"] = sec.line_start
        d["lineEnd"] = sec.line_end
        d["events"] = sec.events
    elif sec.last_event_msg:
        d["lastEvent"] = sec.last_event_msg
    return d


# -----------------------------------------------------------------------------
# Section drill-down
# -----------------------------------------------------------------------------

def cmd_section(args: argparse.Namespace) -> int:
    rdir = _resolve_run_dir(args)
    if rdir is None:
        return 1
    meta = _read_json(rdir / FILE_META, default={})
    if not meta:
        print(json.dumps({"error": f"run {args.run_id} has no meta.json"}), file=sys.stderr)
        return 1
    # Sub-agent runs have no runnerlog sections (parser: "none"). Send a
    # clearer error than the generic "section not found" -- the agent
    # almost certainly wants runner_grep on the conversation logs.
    if meta.get("agentRuntime"):
        print(json.dumps({
            "error": f"run {args.run_id} is a sub-agent conversation with no sections",
            "hint": (
                "Sub-agent runs use append-only stdout.log/stderr.log "
                "without runnerlog markers. Use runner_grep to search "
                "the conversation -- it scopes to the current turn by "
                "default, or pass --all-turns for the full history."
            ),
        }), file=sys.stderr)
        return 1

    events = parse_events(rdir / FILE_STDOUT)
    sections = build_sections(events)
    synth = synthesize_run_state(meta, sections)
    matching = [s for s in sections if s.name == args.name]
    if not matching:
        print(json.dumps({"error": f"section {args.name!r} not found in run {args.run_id}"}), file=sys.stderr)
        return 1

    # Default to the first occurrence; --occurrence to pick another (1-based)
    occ = max(1, args.occurrence or 1) - 1
    if occ >= len(matching):
        print(json.dumps({"error": f"occurrence {args.occurrence} out of range (have {len(matching)})"}), file=sys.stderr)
        return 1

    sec = matching[occ]
    section_dict = _section_to_dict(sec, verbose=True)

    # Filter passing/skipping per-test events for adapter runs unless the
    # caller asked for verbose. The whole point of drilling into a failed
    # section is to see the failures -- 91 passes interleaved with 2 fails
    # is the same context-bomb the runner_status compaction already solves.
    # Always preserved: failed tests, failure-detail lines, non-test events.
    verbose = bool(getattr(args, "verbose", False))
    suppressed_pass = suppressed_skip = 0
    if not verbose and section_dict.get("events"):
        kept_events: list[dict[str, Any]] = []
        for ev in section_dict["events"]:
            ts = ev.get("testStatus")
            if ts == "pass":
                suppressed_pass += 1
                continue
            if ts == "skip":
                suppressed_skip += 1
                continue
            kept_events.append(ev)
        section_dict["events"] = kept_events
        if suppressed_pass + suppressed_skip > 0:
            section_dict["suppressedTestEvents"] = {
                "passed": suppressed_pass,
                "skipped": suppressed_skip,
                "hint": "Per-test pass/skip events hidden. Pass verbose:true to see them.",
            }

    # logTail: last N stdout lines from the section's line range
    log_tail: list[str] = []
    if sec.line_start and sec.line_end:
        log_tail = _read_line_range(rdir / FILE_STDOUT, sec.line_start, sec.line_end, tail=SECTION_LOG_TAIL)

    # stderrTail: last N lines of stderr (no section attribution; stderr isn't
    # marker-bound so we just give the agent the recent N lines).
    stderr_tail = _read_last_lines(rdir / FILE_STDERR, SECTION_STDERR_TAIL)

    response: dict[str, Any] = {
        "runId": meta["runId"],
        "section": section_dict,
        "logTail": log_tail,
        "stderrTail": stderr_tail,
    }
    if verbose:
        response["runRoot"] = str(rdir)

    if getattr(args, "grep", None):
        response["grep"] = _run_grep(
            rdir,
            args.grep,
            stream=getattr(args, "grep_stream", None) or "both",
            a=int(getattr(args, "grep_a", 0) or 0),
            b=int(getattr(args, "grep_b", 0) or 0),
            limit=int(getattr(args, "grep_limit", 0) or 200),
            ignore_case=bool(getattr(args, "grep_ignore_case", False)),
        )

    print(json.dumps(response, indent=2 if args.pretty else None))
    return 0


def _read_line_range(path: Path, start: int, end: int, *, tail: int | None = None) -> list[str]:
    if not path.exists():
        return []
    try:
        lines: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i < start:
                    continue
                if i > end:
                    break
                lines.append(line.rstrip("\n"))
        if tail is not None and len(lines) > tail:
            lines = lines[-tail:]
        return lines
    except OSError:
        return []


# -----------------------------------------------------------------------------
# List + Kill + Purge
# -----------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    """List runs as a scoreboard. For each run, returns enough signal that
    an agent can answer "what's running and is it healthy?" in one call:

      runId, name, state, result, durationSec
      pid, restartCount
      lastLine + lastLineAgeSec  (most recent stdout line and how stale it is)
      stderrCount                (total stderr lines)
      endpoints                  (detected URLs / ports)
      failedSections             (for runs that used sections)

    Discovery is **scoped to the agent's project root** (the git repo
    containing args.cwd, or the runner CLI's cwd if none was passed,
    falling back to the global storage dir if no git root was found). This is
    deliberate isolation: an agent in project A should only see its
    own runs, not runs that another agent in project B started. Two
    agents working in the same project root will see each other's
    runs (assumed coordinated by the engineer).
    """
    seen: set[str] = set()
    run_dirs: list[Path] = []

    # Scope: this agent's project run-root only.
    project_root = find_run_root(Path(args.cwd) if args.cwd else None)
    if project_root.exists():
        for d in sorted(project_root.iterdir(), key=lambda p: p.name, reverse=True):
            if not d.is_dir() or d.name in seen:
                continue
            seen.add(d.name)
            run_dirs.append(d)

    runs: list[dict[str, Any]] = []
    now = int(time.time())
    for d in run_dirs:
        meta = _read_json(d / FILE_META)
        if not meta:
            continue
        # Capture/refresh agent summary for any agent-conversation runs so
        # the scoreboard surfaces sessionId + turn count without the agent
        # having to runner_status each run individually.
        meta = _maybe_capture_agent_session(d, meta)
        events = parse_events(d / FILE_STDOUT, parser_hint=meta.get("parser", "auto"))
        sections = build_sections(events)
        synth = synthesize_run_state(meta, sections)

        # Last stdout line + age (cheap on small files; for huge files we
        # only read the last 4 KB)
        last_line = ""
        last_line_age: int | None = None
        stdout_path = d / FILE_STDOUT
        if stdout_path.exists():
            try:
                size = stdout_path.stat().st_size
                with stdout_path.open("rb") as f:
                    if size > 4096:
                        f.seek(-4096, 2)
                    chunk = f.read()
                tail = chunk.decode("utf-8", errors="replace").splitlines()
                if tail:
                    last_line = tail[-1]
                    last_line_age = max(0, now - int(stdout_path.stat().st_mtime))
            except OSError:
                pass

        stderr_count = _count_lines(d / FILE_STDERR)

        entry: dict[str, Any] = {
            "runId": meta["runId"],
            "name": meta.get("name", "run"),
            "state": synth["state"],
            "result": synth["result"],
            "durationSec": (meta.get("endedAt") or now) - meta.get("startedAt", now),
            "startedAt": meta.get("startedAt"),
            "exitCode": synth["exitCode"],
            "pid": meta.get("pid"),
            "runRoot": str(d),
        }
        if meta.get("description"):
            entry["description"] = meta["description"]
        if meta.get("restartCount"):
            entry["restartCount"] = meta["restartCount"]
        if last_line:
            entry["lastLine"] = last_line
            entry["lastLineAgeSec"] = last_line_age
        if stderr_count:
            entry["stderrCount"] = stderr_count
        endpoints = _detect_endpoints(d)
        if endpoints:
            entry["endpoints"] = endpoints
        failed = [s.name for s in sections if s.status == "failed"]
        if failed:
            entry["failedSections"] = failed
        agent_summary = meta.get("_agentSummary")
        if agent_summary:
            agent_entry: dict[str, Any] = {
                "runtime": agent_summary["runtime"],
                "turn": agent_summary.get("turnCount", 1),
            }
            if agent_summary.get("sessionId"):
                agent_entry["backendSessionId"] = agent_summary["sessionId"]
            entry["agent"] = agent_entry
        runs.append(entry)

    # Filters
    if args.state:
        runs = [r for r in runs if r["state"] == args.state]
    if getattr(args, "name", None):
        try:
            name_rx = re.compile(args.name)
        except re.error:
            name_rx = None
        if name_rx is not None:
            runs = [r for r in runs if name_rx.search(r["name"])]
        else:
            runs = [r for r in runs if args.name in r["name"]]
    # Sort by startedAt desc so the freshest runs are at the top
    runs.sort(key=lambda r: r.get("startedAt") or 0, reverse=True)
    if args.limit:
        runs = runs[:args.limit]

    print(json.dumps(runs, indent=2 if args.pretty else None))
    return 0


def _kill_run_pgroup(rdir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """SIGKILL a run's process group and mark meta as exited.

    Returns the updated meta dict (also writes it to disk). Idempotent
    -- safe to call on an already-dead process. Used by cmd_kill and
    by the poll loop's interrupt-cleanup path.
    """
    pid = meta.get("pid", 0) or 0
    if pid <= 0 or not _process_alive(pid):
        return meta
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    killed_at = int(time.time())
    meta = dict(meta)
    meta["killedAt"] = killed_at
    meta["state"] = "exited"
    if meta.get("exitCode") is None:
        meta["exitCode"] = -9
    if meta.get("endedAt") is None:
        meta["endedAt"] = killed_at
    _atomic_write_json(rdir / FILE_META, meta)
    return meta


def cmd_kill(args: argparse.Namespace) -> int:
    rdir = _resolve_run_dir(args)
    if rdir is None:
        return 1
    meta = _read_json(rdir / FILE_META, default={})
    if not meta:
        print(json.dumps({"error": f"run {args.run_id} has no meta.json"}), file=sys.stderr)
        return 1
    pid = meta.get("pid", 0) or 0
    if pid <= 0 or not _process_alive(pid):
        response: dict[str, Any] = {
            "runId": args.run_id,
            "killed": False,
            "reason": "process not alive",
        }
        _attach_resume_hint(response, meta)
        print(json.dumps(response))
        return 0
    try:
        _kill_run_pgroup(rdir, meta)
    except Exception as e:
        print(json.dumps({"error": f"kill failed: {e}"}), file=sys.stderr)
        return 1
    response = {
        "runId": args.run_id,
        "killed": True,
        "killedAt": int(time.time()),
        "pid": pid,
    }
    _attach_resume_hint(response, meta)
    print(json.dumps(response))
    return 0


def _attach_resume_hint(response: dict[str, Any], meta: dict[str, Any]) -> None:
    """For sub-agent runs, tell the caller the conversation isn't gone.

    Killing a sub-agent run stops the in-flight turn but the backend
    session id is preserved in meta.json. The caller can dispatch a new
    ask on the same runId to resume the conversation -- opencode loads
    -s <sessionId> and the sub-agent picks up its prior memory of the
    conversation. Without this hint, an agent reading the bare
    {killed: true} response might believe the conversation is gone.
    """
    if meta.get("agentRuntime"):
        run_id = meta.get("runId") or response.get("runId")
        response["conversationPreserved"] = True
        response["resumeHint"] = (
            f"The sub-agent's conversation is NOT gone. Backend session id "
            f"is preserved. To resume, dispatch a new turn on the same runId: "
            f"research_and_code_assistant_agent {{ runId: {run_id!r}, "
            f"ask: '<your next instruction>' }}. The sub-agent will see the "
            f"full prior conversation context plus your new message, just "
            f"like a human typing in the middle of a session."
        )


def _run_grep(
    rdir: Path,
    pattern: str,
    *,
    stream: str = "both",
    a: int = 0,
    b: int = 0,
    limit: int = 200,
    ignore_case: bool = False,
    line_bounds: dict[str, tuple[int, int]] | None = None,
    turn_lookup: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Search stdout.log and/or stderr.log for a regex pattern.

    Returns {pattern, matches: [{stream, lineNo, line, context: {before, after}}],
    truncated, totalMatches}. Used both by cmd_grep and as an embedded
    helper from cmd_status / cmd_section / runner_start blocking response.

    line_bounds (optional): {stream_name: (line_start_0indexed, line_end_exclusive)}.
        When present, lines OUTSIDE the bounds for that stream are still
        searched -- but matches outside the bounds are dropped. This keeps
        before/after context working correctly across the boundary (we can
        still pull `b` lines before the first in-bounds line, etc.).

    turn_lookup (optional): the agentTurnCursors array. When provided,
        each match gets a `turn: <N>` field based on which turn's cursor
        range contains its lineNo. Used when callers pass --all-turns
        so the agent can tell which turn produced each hit.
    """
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return {"pattern": pattern, "error": f"invalid regex: {e}", "matches": []}

    streams: list[tuple[str, Path]] = []
    if stream in ("stdout", "both"):
        streams.append(("stdout", rdir / FILE_STDOUT))
    if stream in ("stderr", "both"):
        streams.append(("stderr", rdir / FILE_STDERR))

    matches: list[dict[str, Any]] = []
    total_matches = 0
    truncated = False

    def _turn_of(stream_name: str, line_no_1: int) -> int | None:
        """Resolve a 1-indexed line number to its turn, via the cursor list."""
        if not turn_lookup:
            return None
        line_key = "stdoutLine" if stream_name == "stdout" else "stderrLine"
        # cursors[i].lineKey is the 0-indexed line where turn (i+1) starts.
        # A match at 1-indexed line N (0-indexed N-1) belongs to the LAST
        # turn whose start <= N-1.
        line0 = line_no_1 - 1
        chosen = None
        for c in turn_lookup:
            start = int(c.get(line_key, 0))
            if start <= line0:
                chosen = c
            else:
                break
        if not chosen:
            return None
        turn_v = chosen.get("turn")
        return int(turn_v) if isinstance(turn_v, (int, str)) else None

    for stream_name, path in streams:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip("\n") for l in f.readlines()]
        except OSError:
            continue
        # Determine bounds for THIS stream
        bound = (line_bounds or {}).get(stream_name)
        b_start = bound[0] if bound else 0
        b_end = bound[1] if bound else len(lines)
        if b_end > len(lines):
            b_end = len(lines)
        for i, line in enumerate(lines):
            if rx.search(line):
                # In-bounds check: line index i is 0-indexed
                if i < b_start or i >= b_end:
                    continue
                total_matches += 1
                if len(matches) >= limit:
                    truncated = True
                    continue
                before = lines[max(0, i - b):i] if b > 0 else []
                after = lines[i + 1:i + 1 + a] if a > 0 else []
                entry: dict[str, Any] = {
                    "stream": stream_name,
                    "lineNo": i + 1,
                    "line": line,
                }
                if before or after:
                    entry["context"] = {"before": before, "after": after}
                t = _turn_of(stream_name, i + 1)
                if t is not None:
                    entry["turn"] = t
                matches.append(entry)

    return {
        "pattern": pattern,
        "matches": matches,
        "totalMatches": total_matches,
        "truncated": truncated,
    }


# -----------------------------------------------------------------------------
# Endpoint detection -- find URLs / listen addresses in service logs
# -----------------------------------------------------------------------------

# Catch the most common service-up signals from common toolchains:
#   vite/next/webpack:  "Local:   http://localhost:5801/"
#   go:                 "listening on :5800" / "starting on :8080" / "addr: ":5800""
#   express/koa:        "Server running on port 3000" / "listening at http://..."
#   uvicorn:            "Uvicorn running on http://0.0.0.0:8000"
#   rails:              "Listening on http://127.0.0.1:3000"
_ENDPOINT_REGEXES = [
    re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::\]|\[::1\])(?::\d+)?(?:/\S*)?"),
    re.compile(r"https?://[a-zA-Z0-9.-]+(?::\d+)+(?:/\S*)?"),
    re.compile(r"(?:listening|starting|running|server|bound|bind)\s+(?:on|at|to)\s+:(\d{2,5})\b", re.IGNORECASE),
    re.compile(r"\"addr\":\s*\":(\d{2,5})\"", re.IGNORECASE),
    re.compile(r"port[\s=:]+(\d{2,5})\b", re.IGNORECASE),
]
_ENDPOINT_SCAN_LINES = 200


def _detect_endpoints(rdir: Path) -> list[str]:
    """Scan the first N lines of stdout+stderr for service endpoints.

    Returns a deduplicated list of URL-shaped strings (e.g.
    "http://localhost:5801") or "port:5800" entries when only a port was
    found. Empty list if nothing matches.

    Bounded scan: only the first _ENDPOINT_SCAN_LINES of each stream so
    long-running services with megabyte logs stay cheap.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            found.append(value)

    for path in (rdir / FILE_STDOUT, rdir / FILE_STDERR):
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i >= _ENDPOINT_SCAN_LINES:
                        break
                    line = line.rstrip("\n")
                    for rx in _ENDPOINT_REGEXES:
                        for m in rx.finditer(line):
                            text = m.group(0)
                            # If only a port group, normalize to "port:NNNN"
                            if m.lastindex and m.group(m.lastindex).isdigit() and not text.startswith("http"):
                                add(f"port:{m.group(m.lastindex)}")
                            else:
                                add(text.rstrip(",.;)"))
        except OSError:
            continue
    return found


# -----------------------------------------------------------------------------
# Wait for: block until a regex matches in stdout/stderr
# -----------------------------------------------------------------------------

def cmd_wait_for(args: argparse.Namespace) -> int:
    """Block until a regex appears in the run's logs, or the run exits, or
    BLOCKING_WAIT_SEC elapses. Designed for the "is the service up?"
    pattern after a non-blocking start:

      runner_start { cmd: "go run .", blocking: false } -> runId
      runner_wait_for { runId, pattern: "listening on" }

    Returns one of three outcomes via `outcome`:
      "matched" -- pattern found; `match` has stream/lineNo/line
      "exited"  -- run terminated before pattern appeared (likely crashed)
      "timeout" -- BLOCKING_WAIT_SEC elapsed; run still alive, pattern
                   not seen. Re-call to keep waiting.

    The wait is fixed (NOT user-tunable) because the MCP transport caps
    requests at ~60s. The job is NEVER killed by this tool.
    """
    rdir = _resolve_run_dir(args)
    if rdir is None:
        return 1
    if not args.pattern:
        print(json.dumps({"error": "pattern required"}), file=sys.stderr)
        return 1
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        rx = re.compile(args.pattern, flags)
    except re.error as e:
        print(json.dumps({"error": f"invalid regex: {e}"}), file=sys.stderr)
        return 1

    # Fixed wait sized below the MCP transport limit. The agent can't
    # actually raise that cap, so exposing a timeoutSec knob would be a
    # footgun. On elapse, the run keeps going and the agent re-calls.
    wait_sec = BLOCKING_WAIT_SEC
    stream_arg = args.stream or "both"
    streams: list[tuple[str, Path]] = []
    if stream_arg in ("stdout", "both"):
        streams.append(("stdout", rdir / FILE_STDOUT))
    if stream_arg in ("stderr", "both"):
        streams.append(("stderr", rdir / FILE_STDERR))

    deadline = time.time() + wait_sec
    started_at = time.time()
    poll_sec = 0.25

    def scan() -> dict[str, Any] | None:
        for stream_name, path in streams:
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, start=1):
                        line = line.rstrip("\n")
                        if rx.search(line):
                            return {"stream": stream_name, "lineNo": i, "line": line}
            except OSError:
                continue
        return None

    outcome = "timeout"
    match: dict[str, Any] | None = None
    while True:
        match = scan()
        if match is not None:
            outcome = "matched"
            break
        meta = _read_json(rdir / FILE_META, default={}) or {}
        events = parse_events(rdir / FILE_STDOUT, parser_hint=meta.get("parser", "auto"))
        sections = build_sections(events)
        synth = synthesize_run_state(meta, sections)
        if synth["state"] == "exited":
            outcome = "exited"
            break
        if time.time() >= deadline:
            outcome = "timeout"
            break
        time.sleep(poll_sec)

    elapsed = round(time.time() - started_at, 2)
    response: dict[str, Any] = {
        "runId": args.run_id,
        "outcome": outcome,
        "pattern": args.pattern,
        "elapsedSec": elapsed,
        "waitSec": wait_sec,
        "runRoot": str(rdir),
    }
    if outcome == "matched":
        response["match"] = match
        # Bonus: surface detected endpoints so the agent immediately knows
        # what URL/port the service is on.
        endpoints = _detect_endpoints(rdir)
        if endpoints:
            response["endpoints"] = endpoints
    elif outcome == "exited":
        meta = _read_json(rdir / FILE_META, default={}) or {}
        response["exitCode"] = meta.get("exitCode")
        response["result"] = meta.get("result")
        response["stderrTail"] = _read_last_lines(rdir / FILE_STDERR, 10)
        response["followUp"] = (
            "Run exited before the pattern appeared. Likely crashed. "
            "Inspect stderrTail above or call runner_grep for more context."
        )
    else:
        # Sub-agent hint: tell the agent to use the dispatch tool for
        # polling, not wait_for. wait_for is generic; the dispatch tool
        # is the right shape for "give me the agent's reply when ready."
        meta = _read_json(rdir / FILE_META, default={}) or {}
        if meta.get("agentRuntime"):
            response["followUp"] = (
                f"Pattern not seen in {elapsed}s. For sub-agent runs, "
                f"call research_and_code_assistant_agent with just runId "
                f"(no ask) to poll for the response -- it returns the "
                f"finalReply directly once the turn completes. Or call "
                f"runner_wait_for again to keep waiting for this pattern."
            )
        else:
            response["followUp"] = (
                f"Pattern not seen in {elapsed}s. The run is STILL ACTIVE "
                f"(NOT killed). Call runner_wait_for again with the same runId "
                f"to keep waiting, or runner_status to inspect, or runner_kill "
                f"to abort."
            )

    print(json.dumps(response, indent=2 if args.pretty else None))
    return 0


def cmd_grep(args: argparse.Namespace) -> int:
    rdir = _resolve_run_dir(args)
    if rdir is None:
        return 1
    if not args.pattern:
        print(json.dumps({"error": "pattern required"}), file=sys.stderr)
        return 1

    # Agent-aware turn scoping: when the run is a sub-agent conversation,
    # the default is to scope the grep to the CURRENT turn only (most
    # recent turn's slice of the append-only logs). Pass --all-turns to
    # search the whole conversation; matches are then annotated with
    # `turn: N` so the agent can place each hit.
    line_bounds: dict[str, tuple[int, int]] | None = None
    turn_lookup: list[dict[str, Any]] | None = None
    meta = _read_json(rdir / FILE_META, default={}) or {}
    if meta.get("agentRuntime"):
        cursors = meta.get("agentTurnCursors") or []
        if getattr(args, "all_turns", False):
            # Span the whole conversation; annotate with turn numbers.
            turn_lookup = cursors if cursors else None
        elif cursors:
            # Scope to current turn (last cursor entry -> EOF for each stream).
            agents_mod = _agents_module()
            if agents_mod is not None and hasattr(agents_mod, "turn_line_bounds"):
                line_bounds = {
                    "stdout": agents_mod.turn_line_bounds(meta, "stdout"),
                    "stderr": agents_mod.turn_line_bounds(meta, "stderr"),
                }

    result = _run_grep(
        rdir,
        args.pattern,
        stream=args.stream or "both",
        a=int(args.a or 0),
        b=int(args.b or 0),
        limit=int(args.limit or 200),
        ignore_case=bool(args.ignore_case),
        line_bounds=line_bounds,
        turn_lookup=turn_lookup,
    )
    result["runId"] = args.run_id
    result["runRoot"] = str(rdir)
    if line_bounds is not None:
        # Tell the agent we filtered to the current turn so it knows
        # to pass --all-turns if it wants the full history.
        result["scope"] = "current-turn"
        result["currentTurn"] = meta.get("agentTurnCount")
    elif turn_lookup is not None:
        result["scope"] = "all-turns"
        result["totalTurns"] = len(turn_lookup)
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """Remove run directories with a structured report of what happened.

    Modes (mutually exclusive, in order of precedence):

      runId given       -> remove that single run regardless of state.
                           Resolves via the global index so cross-project
                           runIds work.

      olderThan: N      -> remove TERMINAL runs whose endedAt is older
                           than N seconds, scoped to the agent's project
                           root. Active runs are never touched.

      result: success   -> remove all terminal runs with result=success.
      result: failed    -> remove all terminal runs with result=failed.

      (no args)         -> remove ALL terminal runs in the agent's
                           project root. Active runs are reported as
                           kept.

    The response is a structured report:
      {
        purged:      [{ runId, name, result, durationSec, freedBytes }],
        kept: {
          active:    [{ runId, name, state, pid, durationSec }],
          filtered:  [{ runId, name, result, reason }]    // didn't match the filter
        },
        freedBytesTotal: N,
        scope: "<runRoot>"
      }
    """
    purged: list[dict[str, Any]] = []
    kept_active: list[dict[str, Any]] = []
    kept_filtered: list[dict[str, Any]] = []
    freed_total = 0

    # Mode 1: explicit runId -- remove that one regardless of state.
    if args.run_id:
        target: Path | None = _index_lookup(args.run_id)
        if target is None:
            run_root = find_run_root(Path(args.cwd) if args.cwd else None)
            candidate = run_root / args.run_id
            if candidate.exists():
                target = candidate
        if target is not None and target.exists():
            meta = _read_json(target / FILE_META) or {}
            size = _dir_size(target)
            _rmtree(target)
            freed_total += size
            purged.append({
                "runId": args.run_id,
                "name": meta.get("name", "run"),
                "result": meta.get("result"),
                "durationSec": (meta.get("endedAt") or int(time.time())) - (meta.get("startedAt") or int(time.time())),
                "freedBytes": size,
            })
        print(json.dumps({
            "purged": purged,
            "kept": {"active": [], "filtered": []},
            "freedBytesTotal": freed_total,
        }, indent=2 if args.pretty else None))
        return 0

    # Modes 2-4: scoped to agent's project root.
    run_root = find_run_root(Path(args.cwd) if args.cwd else None)
    older_than = int(args.older_than or 0)
    result_filter = (args.result or "").strip() or None
    threshold = int(time.time()) - older_than if older_than > 0 else None
    now = int(time.time())

    if run_root.exists():
        for d in sorted(run_root.iterdir(), key=lambda p: p.name):
            if not d.is_dir():
                continue
            meta = _read_json(d / FILE_META)
            if not meta:
                continue
            run_id = d.name
            name = meta.get("name", "run")
            ended = meta.get("endedAt")
            result = meta.get("result")

            # Active runs are never purged
            if ended is None:
                # Re-synthesize state in case the daemon crashed without
                # finalizing meta -- otherwise we'd report a long-dead run
                # as "active" forever.
                events = parse_events(d / FILE_STDOUT, parser_hint=meta.get("parser", "auto"))
                sections = build_sections(events)
                synth = synthesize_run_state(meta, sections)
                if synth["state"] != "exited":
                    kept_active.append({
                        "runId": run_id,
                        "name": name,
                        "state": synth["state"],
                        "pid": meta.get("pid"),
                        "durationSec": now - (meta.get("startedAt") or now),
                    })
                    continue
                # Synthesized terminal state -- treat as terminal for purge
                ended = synth.get("endedAt") or now
                result = synth.get("result") or result

            # Filter: olderThan
            if threshold is not None and ended > threshold:
                kept_filtered.append({
                    "runId": run_id,
                    "name": name,
                    "result": result,
                    "reason": f"younger than {older_than}s (ended {now - ended}s ago)",
                })
                continue

            # Filter: result
            if result_filter and result != result_filter:
                kept_filtered.append({
                    "runId": run_id,
                    "name": name,
                    "result": result,
                    "reason": f"result={result!r}, filter wants {result_filter!r}",
                })
                continue

            # Purge
            size = _dir_size(d)
            _rmtree(d)
            freed_total += size
            purged.append({
                "runId": run_id,
                "name": name,
                "result": result,
                "durationSec": ended - (meta.get("startedAt") or ended),
                "freedBytes": size,
            })

    response = {
        "purged": purged,
        "kept": {
            "active": kept_active,
            "filtered": kept_filtered,
        },
        "freedBytesTotal": freed_total,
        "scope": str(run_root),
        "summary": {
            "purgedCount": len(purged),
            "keptActiveCount": len(kept_active),
            "keptFilteredCount": len(kept_filtered),
        },
    }
    print(json.dumps(response, indent=2 if args.pretty else None))
    return 0


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# -----------------------------------------------------------------------------
# Helpers tool: surface library paths for script authors
# -----------------------------------------------------------------------------

def cmd_guide(args: argparse.Namespace) -> int:
    """Return the agent-facing usage guide as text.

    The MCP exposes this as `runner_guide`. Agents should call this FIRST
    when they're not sure which tool to use; it explains the three core
    workflows (one-shot tests/builds, long-running services, instrumented
    scripts) and shows the response shapes they'll see.
    """
    install_root = Path(__file__).resolve().parent.parent
    guide_path = install_root / "docs" / "GUIDE.md"
    if not guide_path.exists():
        print(json.dumps({"error": f"guide not found at {guide_path}"}), file=sys.stderr)
        return 1
    try:
        content = guide_path.read_text(encoding="utf-8")
    except OSError as e:
        print(json.dumps({"error": f"failed to read guide: {e}"}), file=sys.stderr)
        return 1
    # Print the markdown directly. The MCP server wraps it in tool-text.
    print(content)
    return 0


def cmd_helpers(args: argparse.Namespace) -> int:
    """Return JSON describing where the runnerlog library lives + snippets.

    The MCP exposes this as `runner_helpers` so an agent calls it once and
    learns how to instrument a script.
    """
    install_root = Path(__file__).resolve().parent.parent  # install dest (XDG data dir) OR source tree
    lib = install_root / "lib"
    response = {
        "bash": {
            "sourceLine": f'source "{lib / "runnerlog.sh"}"',
            "absolutePath": str(lib / "runnerlog.sh"),
            "snippet": _BASH_SNIPPET.replace("@LIB_SH@", str(lib / "runnerlog.sh")),
        },
        "python": {
            "sysPath": str(lib),
            "importLine": "import runnerlog",
            "absolutePath": str(lib / "runnerlog.py"),
            "snippet": _PYTHON_SNIPPET.replace("@LIB_DIR@", str(lib)),
        },
        "cli": {
            "command": "runnerlog",
            "absolutePath": str(lib / "runnerlog"),
            "snippet": _CLI_SNIPPET,
            "note": "On PATH after `make install` (symlinked to ~/.local/bin/runnerlog).",
        },
        "guide": str(install_root / "docs" / "GUIDE.md"),
    }
    print(json.dumps(response, indent=2 if args.pretty else None))
    return 0


_BASH_SNIPPET = '''\
# Source once at the top of your script:
source "@LIB_SH@"

runnerlog_section_start "compile"
go build ./... && rc=$? || rc=$?
if [ $rc -eq 0 ]; then
  runnerlog_section_end "compile" ok exit=$rc
else
  runnerlog_section_end "compile" failed exit=$rc reason="build failed"
fi

runnerlog_metric files=42 bytes=12345
runnerlog_event "doc 1 of 19 complete"
'''

_PYTHON_SNIPPET = '''\
import sys, os
sys.path.insert(0, "@LIB_DIR@")
import runnerlog

with runnerlog.section("compile"):
    # ... do work ...
    runnerlog.metric(files=42, bytes=12345)
    runnerlog.event("doc 1 of 19 complete")
'''

_CLI_SNIPPET = '''\
runnerlog section_start compile
go build ./...
rc=$?
runnerlog section_end compile ok exit=$rc
runnerlog metric files=42
runnerlog event "doc 1 of 19 complete"
'''


# -----------------------------------------------------------------------------
# Sub-agent conversations (research_and_code_assistant_agent MCP tool)
# -----------------------------------------------------------------------------

def _find_run_wrapping_session(run_root: Path, runtime_name: str, session_id: str) -> Path | None:
    """Scan run_root for a runner run already wrapping this backend session.

    Used by the adoption codepath in cmd_agent: if the agent passes a
    backend session id but runner already has a runId wrapping it (from a
    previous successful adoption or first-turn capture), we point them at
    that existing runId rather than create a parallel duplicate. Two
    runner runs claiming the same backend session is a footgun -- each
    turn from either would wipe the other's transcript.
    """
    if not run_root.exists():
        return None
    for d in sorted(run_root.iterdir()):
        if not d.is_dir():
            continue
        meta = _read_json(d / FILE_META, default={}) or {}
        if meta.get("agentRuntime") != runtime_name:
            continue
        if meta.get("agentSessionId") == session_id:
            return d
        # The session id may not have been persisted yet (lazy capture);
        # check the backend stderr/NDJSON via the runtime's extractor.
        agents = _agents_module()
        if agents is None:
            continue
        runtime = agents.get(runtime_name)
        if runtime is None:
            continue
        try:
            info = runtime.extract(d) or {}
        except Exception:
            info = {}
        if info.get("sessionId") == session_id:
            return d
    return None


def cmd_agent(args: argparse.Namespace) -> int:
    """Start, continue, adopt, or poll a sub-agent conversation.

    Four modes, distinguished by which of --ask / --run-id are set:

      1. NEW CONVERSATION (--ask, no --run-id):
         Fresh runId, fresh run dir, no `-s` flag. The backend picks
         its own session id; runner extracts it lazily on first read.

      2. CONTINUATION (--ask + --run-id is a UUIDv7):
         Resolve the existing run dir, read meta.agentSessionId, wipe
         per-turn output files, archive the previous prompt to
         prompts/<turn>.md, write the new prompt, respawn with -s.
         Same runId across all turns.

      3. ADOPT EXISTING BACKEND SESSION (--ask + --run-id is non-UUIDv7):
         Caller passed a backend session id for a conversation that
         exists outside runner. Allocate a fresh runner runId, spawn
         with `-s <that-session-id>`. Errors flow through to
         stderr.log naturally; runner does not classify them.

      4. POLL FOR RESPONSE (--run-id, NO --ask):
         "Is the previous turn done? Give me the answer." Resolves
         the existing runner run, then blocks up to ~45s for the
         turn to reach a final reply (or for the process to exit).
         On terminal returns the focused finalReply. On timeout
         returns the stillRunning + progress block + call-again hint.
         The agent never needs runner_wait_for or runner_status for
         sub-agent polling -- the dispatch tool IS the polling tool.

    Always blocking: a sub-agent invocation is a single short
    interaction, so the response is the final status snapshot (or
    the stillRunning marker if the wait window elapsed).
    """
    agents = _agents_module()
    if agents is None:
        print(json.dumps({
            "error": "agent runtimes unavailable: lib/agents package not importable",
            "hint": f"ensure {_AGENTS_DIR}/agents/__init__.py exists (re-run the installer)",
        }), file=sys.stderr)
        return 1
    runtime_name = (getattr(args, "agent", None) or "opencode").strip()
    runtime = agents.get(runtime_name)
    if runtime is None:
        available = agents.names() if hasattr(agents, "names") else []
        print(json.dumps({
            "error": f"unknown agent runtime: {runtime_name!r}",
            "available": available,
        }), file=sys.stderr)
        return 2

    raw_run_id = getattr(args, "run_id", None)
    ask_text = getattr(args, "ask", None)

    # Mode 4: POLL. --run-id without --ask = "pull for response on the
    # existing run." Skip all spawn logic and jump to the standard
    # block-poll loop on the existing rdir.
    if raw_run_id and not ask_text:
        if not _is_runner_run_id(raw_run_id):
            print(json.dumps({
                "error": f"polling mode requires a runner runId; got {raw_run_id!r}. "
                         f"To adopt a backend session, also pass --ask with a prompt.",
            }), file=sys.stderr)
            return 2
        resolve_ns = argparse.Namespace(run_id=raw_run_id, cwd=getattr(args, "cwd", None))
        rdir = _resolve_run_dir(resolve_ns)
        if rdir is None:
            return 1
        meta_existing = _read_json(rdir / FILE_META, default={}) or {}
        if not meta_existing.get("agentRuntime"):
            print(json.dumps({
                "error": f"run {raw_run_id} is not a sub-agent run "
                         f"(meta.agentRuntime is not set); use runner_status to "
                         f"poll regular runs.",
            }), file=sys.stderr)
            return 1
        start_payload = {
            "runId": raw_run_id,
            "name": meta_existing.get("name"),
            "runRoot": str(rdir),
            "polling": True,
        }
        return _block_until_terminal(rdir, start_payload, getattr(args, "pretty", False))

    if not ask_text:
        print(json.dumps({
            "error": "either --ask (to send a turn) or --run-id alone (to poll for the response) is required",
        }), file=sys.stderr)
        return 2

    run_root = find_run_root(Path(args.cwd) if getattr(args, "cwd", None) else None)
    run_root.mkdir(parents=True, exist_ok=True)

    # Resolve mode: new / continuation / adoption
    rdir: Path | None = None
    meta: dict[str, Any] = {}
    adopting_session_id: str | None = None
    raw_run_id = getattr(args, "run_id", None)
    if raw_run_id:
        if _is_runner_run_id(raw_run_id):
            # Mode 2: continuation. Look up the existing runner run.
            resolve_ns = argparse.Namespace(run_id=raw_run_id, cwd=getattr(args, "cwd", None))
            rdir = _resolve_run_dir(resolve_ns)
            if rdir is None:
                # _resolve_run_dir already printed a structured error
                return 1
            meta = _read_json(rdir / FILE_META, default={}) or {}
            if meta.get("agentRuntime") and meta["agentRuntime"] != runtime_name:
                print(json.dumps({
                    "error": f"run {raw_run_id} is a {meta['agentRuntime']} conversation, "
                             f"cannot continue with {runtime_name}",
                }), file=sys.stderr)
                return 1
        else:
            # Mode 3: adoption. Treat raw_run_id as a backend session id.
            # First check if runner already wraps it; if so, redirect.
            existing = _find_run_wrapping_session(run_root, runtime_name, raw_run_id)
            if existing is not None:
                existing_meta = _read_json(existing / FILE_META, default={}) or {}
                print(json.dumps({
                    "error": f"backend session {raw_run_id!r} is already wrapped by "
                             f"runner run {existing_meta.get('runId')}",
                    "existingRunId": existing_meta.get("runId"),
                    "hint": f"use runId {existing_meta.get('runId')!r} to continue this conversation",
                }), file=sys.stderr)
                return 1
            adopting_session_id = raw_run_id
            # Fall through to the fresh-conversation path with the session
            # id pre-populated so build_cmd uses -s on the first spawn.

    # Continuation path: capture session id from prior turn's logs now (so
    # the cmd we build below has -s available).
    if rdir is not None:
        meta = _maybe_capture_agent_session(rdir, meta)

    backend_session_id = (
        meta.get("agentSessionId") if rdir is not None
        else adopting_session_id  # adoption: spawn with -s on the FIRST turn
    )
    turn_count = int(meta.get("agentTurnCount", 0)) + 1

    if rdir is None:
        # Fresh conversation OR adoption (same scaffold path)
        run_id = gen_run_id()
        rdir = run_dir(run_root, run_id)
        rdir.mkdir(parents=True, exist_ok=True)
        base_name = (
            f"{runtime_name}-adopted" if adopting_session_id
            else f"{runtime_name}-agent"
        )
        name = _next_unique_name(run_root, base_name, always_suffix=True)
    else:
        # Continuation: keep identity, wipe per-turn output
        run_id = meta.get("runId") or args.run_id
        name = meta.get("name") or f"{runtime_name}-agent"
        # Kill any prior turn's process group (defensive; agent turns are
        # short, but a previous turn might still be wrapping up).
        pid = meta.get("pid", 0) or 0
        if pid > 0 and _process_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(20):
                if not _process_alive(pid):
                    break
                time.sleep(0.05)
        # Archive previous prompt so a sequence is preserved for future
        # debugging, then wipe per-turn output. Archive lives under
        # prompts/<turn>.md keyed by the COMPLETED turn number.
        prompts_dir = rdir / "prompts"
        prompts_dir.mkdir(exist_ok=True)
        prev_prompt = rdir / "prompt.md"
        if prev_prompt.exists():
            completed = int(meta.get("agentTurnCount", 0))
            if completed > 0:
                try:
                    prev_prompt.rename(prompts_dir / f"{completed}.md")
                except OSError:
                    # Fallback: copy contents and unlink
                    try:
                        (prompts_dir / f"{completed}.md").write_text(
                            prev_prompt.read_text(encoding="utf-8"), encoding="utf-8")
                        prev_prompt.unlink()
                    except OSError:
                        pass
        # NEW: do NOT wipe stdout.log/stderr.log. The logs are append-only
        # across the entire conversation -- a `tail -f` survives turn
        # boundaries and the operator can scrollback through every turn.
        # tracker.json is delta-cursor state for runner_status; clearing
        # it forces a clean status snapshot for the new turn. pid is
        # recreated by _spawn_into.
        for f in (FILE_TRACKER, FILE_PID):
            p = rdir / f
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    # Write the new turn's prompt. opencode/claude consume this via stdin
    # redirection in the spawned shell pipeline (see runtime.build_cmd).
    prompt_path = rdir / "prompt.md"
    try:
        prompt_path.write_text(args.ask, encoding="utf-8")
    except OSError as e:
        print(json.dumps({"error": f"failed to write prompt.md: {e}"}), file=sys.stderr)
        return 1

    # Build the backend invocation command. The renderer in the pipeline
    # converts NDJSON to readable text on stdout and mirrors raw NDJSON
    # to stderr -- both captured by runner's normal stdout.log / stderr.log
    # routing, so all the existing tools (status / grep / section) work.
    cmd_to_run = runtime.build_cmd(prompt_path, backend_session_id)
    started_at = int(time.time())

    # Capture turn-start cursors BEFORE spawn. Since logs are append-only
    # across the conversation, the current EOF of each log is where this
    # turn's output will begin. Stored in meta.agentTurnCursors so all
    # readers (compact_view, runner_grep, runner_section) can scope to
    # the right turn slice.
    prior_cursors = list(meta.get("agentTurnCursors") or [])
    def _file_size(p: Path) -> int:
        try:
            return p.stat().st_size if p.exists() else 0
        except OSError:
            return 0
    new_cursor = {
        "turn": turn_count,
        "startedAt": started_at,
        "stdoutByte": _file_size(rdir / FILE_STDOUT),
        "stderrByte": _file_size(rdir / FILE_STDERR),
        "stdoutLine": _count_lines(rdir / FILE_STDOUT),
        "stderrLine": _count_lines(rdir / FILE_STDERR),
    }
    turn_cursors = prior_cursors + [new_cursor]

    # Compose meta. Identity fields mirror cmd_start's; agent-specific
    # fields use the `agent*` prefix so they never collide with the
    # generic run schema.
    new_meta: dict[str, Any] = {
        "runId": run_id,
        "name": name,
        "description": meta.get("description") or f"{runtime_name} sub-agent conversation",
        "cmd": cmd_to_run,
        "cwd": (getattr(args, "cwd", None) or meta.get("cwd") or os.getcwd()),
        "startedAt": started_at,
        "endedAt": None,
        "exitCode": None,
        "state": "starting",
        "result": None,
        "fatalMsg": None,
        "killedAt": None,
        "runRoot": str(rdir),
        # Parser stays "none": the renderer produces plain text, not
        # runnerlog markers, so the marker parser has nothing to do.
        "parser": "none",
        "restartCount": int(meta.get("restartCount", 0)),
        # Sub-agent runs always block: a turn is a single short interaction.
        "blockingMode": True,
        "agentRuntime": runtime_name,
        "agentSessionId": backend_session_id,   # may be None on first turn
        "agentTurnCount": turn_count,
        "agentTurnCursors": turn_cursors,
    }
    _atomic_write_json(rdir / FILE_META, new_meta)

    pid = _spawn_into(rdir, cmd_to_run, new_meta["cwd"], name, run_id)
    if pid < 0:
        print(json.dumps({"error": "spawn failed"}), file=sys.stderr)
        return 1

    start_payload: dict[str, Any] = {
        "runId": run_id,
        "pid": pid,
        "name": name,
        "startedAt": started_at,
        "runRoot": str(rdir),
        "blocking": bool(getattr(args, "blocking", True)),
        "agent": {
            "runtime": runtime_name,
            "turn": turn_count,
            "continued": backend_session_id is not None,
        },
    }

    if not getattr(args, "blocking", True):
        # Fire-and-forget: return immediately with the runId so the
        # caller can poll later (or dispatch other work in parallel).
        # The spawned process continues; the caller polls via
        # research_and_code_assistant_agent with just runId, no ask.
        start_payload["stillRunning"] = True
        start_payload["followUp"] = (
            f"Sub-agent dispatched in background. Call "
            f"research_and_code_assistant_agent with just runId="
            f"{run_id!r} (no ask) to poll for the response when ready."
        )
        print(json.dumps(start_payload, indent=2 if getattr(args, "pretty", False) else None))
        return 0

    return _block_until_terminal(rdir, start_payload, getattr(args, "pretty", False))


# -----------------------------------------------------------------------------
# Wire-format emitter (used by the runnerlog CLI shim in lib/runnerlog)
# -----------------------------------------------------------------------------

def cmd_emit(args: argparse.Namespace) -> int:
    """Emit a single ::run:: protocol line to stdout.

    Author-facing: this is what `runnerlog <verb> ...` ultimately invokes.
    Centralizes the JSON serialization in one place so every helper (bash,
    python, CLI) produces byte-identical output.
    """
    verb = args.verb
    if verb not in EVENT_VERBS:
        print(f"runnerlog: unknown verb {verb!r}; valid: {sorted(EVENT_VERBS)}", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {"v": verb, "ts": int(time.time())}

    # Positional: first non-key=value arg is the section name (for
    # section_start/section_end), the message (for event/fail), or unused
    # (for metric).
    positional: list[str] = []
    kvs: dict[str, Any] = {}
    for raw in args.fields:
        if "=" in raw and not raw.startswith("="):
            k, v = raw.split("=", 1)
            kvs[k] = _coerce_value(v)
        else:
            positional.append(raw)

    if verb in ("section_start", "section_end"):
        if positional:
            payload["name"] = positional[0]
            positional = positional[1:]
        if verb == "section_end":
            # Second positional becomes status
            if positional:
                payload["status"] = positional[0]
                positional = positional[1:]
    elif verb in ("event", "fail"):
        # All positional joined as message
        if positional:
            payload["msg"] = " ".join(positional)
            positional = []

    # Remaining positional get folded into kvs as bare flags (rare)
    for p in positional:
        kvs[p] = True

    payload.update(kvs)

    sys.stdout.write(WIRE_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _coerce_value(s: str) -> Any:
    """Coerce a CLI value string to int/float/bool/str."""
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


# -----------------------------------------------------------------------------
# Helpers shared across subcommands
# -----------------------------------------------------------------------------

def _resolve_run_dir(args: argparse.Namespace) -> Path | None:
    """Resolve the run directory from --run-id.

    Searches in this order:
      1. <cwd-derived run root>/<runId>/    (local project .runner)
      2. <global storage>/<runId>/           (global fallback used when start
         was invoked outside any git repo; see RUNNER_GLOBAL)
      3. Also walks up from cwd looking at every parent's .runner/ in case
         the call site is a subdirectory of the original run's git project.

    This makes status calls work without the agent having to remember
    where the run was originally started from.
    """
    if not args.run_id:
        print(json.dumps({"error": "run_id required"}), file=sys.stderr)
        return None

    # Fast path: the global index records every run's location at spawn time.
    indexed = _index_lookup(args.run_id)
    if indexed is not None:
        return indexed

    # Slow path: walk plausible run roots in case the index was lost or this
    # run predates index support. Order: cwd-derived, global, parent dirs.
    candidates: list[Path] = []
    cwd = Path(args.cwd).resolve() if getattr(args, "cwd", None) else Path.cwd().resolve()
    candidates.append(find_run_root(cwd) / args.run_id)
    candidates.append(RUNNER_GLOBAL / args.run_id)
    cur = cwd
    while cur != cur.parent:
        candidates.append(cur / RUNNER_DIRNAME / args.run_id)
        cur = cur.parent

    for rdir in candidates:
        if rdir.exists():
            # Backfill the index so subsequent lookups are fast
            _index_register(args.run_id, rdir)
            return rdir

    print(json.dumps({"error": f"run {args.run_id} not found in index or {[str(c.parent) for c in candidates]}"}), file=sys.stderr)
    return None


# -----------------------------------------------------------------------------
# CLI entry point
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runner",
        description="runner -- fire-and-forget script execution with structured telemetry",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # start
    p = sub.add_parser("start", help="spawn a script detached")
    p.add_argument("--cmd", required=True, help="full shell command to execute")
    p.add_argument("--cwd", help="working directory (default: current)")
    p.add_argument("--name", help="short label for this run (default: auto-derived from cmd)")
    p.add_argument("--description", help="optional free-text description of what the run is doing or why")
    p.add_argument("--blocking", action="store_true", help=f"hold the response until terminal or {BLOCKING_WAIT_SEC}s elapses (never kills the job)")
    p.add_argument("--no-blocking", dest="blocking", action="store_false", help="fire-and-forget; return immediately")
    p.set_defaults(blocking=True)
    p.add_argument("--parser", choices=["auto", "none", "go-test"], default="auto", help="output parser: auto (default), none (markers only), or a specific adapter name")
    p.add_argument("--no-scrub", action="store_true", help="disable cmd scrubbing (skip stripping pipes to head/tail/grep/etc)")
    p.add_argument("--pretty", action="store_true", help="pretty-print JSON (used in blocking response)")
    p.set_defaults(func=cmd_start)

    # restart
    p = sub.add_parser("restart", help="kill + respawn an existing run under the same runId")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cwd", help="working directory used to find run root")
    p.set_defaults(func=cmd_restart)

    # status
    p = sub.add_parser("status", help="get run status (delta-aware; auto-waits for blocking-mode runs)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--agent", help="agent identifier for delta tracking (default: 'default')")
    p.add_argument("--since", type=int, help="line cursor to use instead of tracker")
    p.add_argument("--cwd", help="working directory used to find run root")
    p.add_argument("--verbose", action="store_true", help="include line numbers, full sections, raw events, meta")
    p.add_argument("--wait", dest="wait", action="store_true", default=None, help="force auto-wait (default: yes for blocking-mode runs, no for services)")
    p.add_argument("--no-wait", dest="wait", action="store_false", help="return immediately even if the run is blocking-mode")
    p.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    p.add_argument("--grep", help="optional regex; embeds grep matches in the response")
    p.add_argument("--grep-stream", choices=["stdout", "stderr", "both"], default="both")
    p.add_argument("--grep-a", type=int, default=0, help="lines of context after each match")
    p.add_argument("--grep-b", type=int, default=0, help="lines of context before each match")
    p.add_argument("--grep-limit", type=int, default=200)
    p.add_argument("--grep-ignore-case", action="store_true")
    p.set_defaults(func=cmd_status)

    # section
    p = sub.add_parser("section", help="drill into a single section")
    p.add_argument("--run-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--occurrence", type=int, help="1-based occurrence (default: 1)")
    p.add_argument("--cwd", help="working directory used to find run root")
    p.add_argument("--verbose", action="store_true", help="include passing/skipping per-test events for adapter-driven runs")
    p.add_argument("--pretty", action="store_true")
    p.add_argument("--grep", help="optional regex; embeds grep matches in the response")
    p.add_argument("--grep-stream", choices=["stdout", "stderr", "both"], default="both")
    p.add_argument("--grep-a", type=int, default=0)
    p.add_argument("--grep-b", type=int, default=0)
    p.add_argument("--grep-limit", type=int, default=200)
    p.add_argument("--grep-ignore-case", action="store_true")
    p.set_defaults(func=cmd_section)

    # wait-for
    p = sub.add_parser("wait-for", help=f"block until a regex appears in logs (or run exits / {BLOCKING_WAIT_SEC}s elapses)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--pattern", required=True, help="regex to look for")
    p.add_argument("--stream", choices=["stdout", "stderr", "both"], default="both")
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--cwd", help="working directory used to find run root")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_wait_for)

    # grep (standalone)
    p = sub.add_parser("grep", help="search stdout.log + stderr.log for a regex")
    p.add_argument("--run-id", required=True)
    p.add_argument("--pattern", required=True)
    p.add_argument("--stream", choices=["stdout", "stderr", "both"], default="both")
    p.add_argument("--A", dest="a", type=int, default=0, help="lines of context after each match")
    p.add_argument("--B", dest="b", type=int, default=0, help="lines of context before each match")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--all-turns", dest="all_turns", action="store_true",
                   help="for sub-agent runs, search the entire conversation history "
                        "(all turns). Default scopes to the current turn only. "
                        "Matches in --all-turns mode are annotated with `turn: N`.")
    p.add_argument("--cwd", help="working directory used to find run root")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_grep)

    # list
    p = sub.add_parser("list", help="list runs (scoreboard with health signals)")
    p.add_argument("--cwd", help="working directory used to find run root")
    p.add_argument("--state", choices=["starting", "running", "exited"])
    p.add_argument("--name", help="regex (or substring) to filter by name")
    p.add_argument("--limit", type=int)
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_list)

    # kill
    p = sub.add_parser("kill", help="kill a running script (SIGKILL on process group)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cwd", help="working directory used to find run root")
    p.set_defaults(func=cmd_kill)

    # purge
    p = sub.add_parser("purge", help="remove run directories with a structured report")
    p.add_argument("--run-id", help="purge a single run by id (resolved via global index)")
    p.add_argument("--older-than", type=int, default=0, help="purge terminal runs whose endedAt is older than N seconds")
    p.add_argument("--result", choices=["success", "failed"], help="filter to terminal runs with this result")
    p.add_argument("--cwd", help="working directory used to find run root")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_purge)

    # guide
    p = sub.add_parser("guide", help="print the agent-facing usage guide (markdown)")
    p.set_defaults(func=cmd_guide)

    # helpers
    p = sub.add_parser("helpers", help="describe where the runnerlog library lives")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_helpers)

    # emit (used by lib/runnerlog CLI shim)
    p = sub.add_parser("emit", help="emit one ::run:: protocol line (used by runnerlog helper)")
    p.add_argument("verb")
    p.add_argument("fields", nargs="*")
    p.set_defaults(func=cmd_emit)

    # agent (sub-agent conversations via research_and_code_assistant_agent)
    p = sub.add_parser(
        "agent",
        help="start, continue, adopt, or poll a sub-agent conversation",
    )
    p.add_argument("--agent", default="opencode",
                   help="agent runtime name (default: opencode)")
    p.add_argument("--ask",
                   help="prompt to send to the sub-agent. omit (with --run-id) to poll for the response on an existing run.")
    p.add_argument("--run-id",
                   help="continue an existing conversation (UUIDv7), adopt an external backend session (when --ask is also set), or poll for the response on an existing agent run (when --ask is omitted)")
    p.add_argument("--cwd",
                   help="working directory for the sub-agent process")
    p.add_argument("--blocking", action="store_true", default=True,
                   help=f"hold the response until the turn completes or {BLOCKING_WAIT_SEC}s elapses (default)")
    p.add_argument("--no-blocking", dest="blocking", action="store_false",
                   help="fire-and-forget; return immediately with the runId so the caller can poll later")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_agent)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
