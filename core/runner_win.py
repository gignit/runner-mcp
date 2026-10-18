"""
runner_win -- the Windows half of runner's process model.

runner_core.py detaches, captures, and kills runs with POSIX primitives
(double fork + setsid, two ptys, killpg). None of those exist on Windows,
so this module supplies the equivalents and runner_core.py calls into it
when os.name == "nt":

  POSIX                         Windows (this module)
  ----------------------------  ------------------------------------------
  double fork + setsid          a detached SUPERVISOR process
                                (`runner_core.py _supervise`) launched with
                                CREATE_BREAKAWAY_FROM_JOB so it escapes the
                                agent's Job Object (libuv and most agent
                                hosts put children in a kill-on-close job:
                                without breakaway every run dies with the
                                session). If the job forbids breakaway, the
                                supervisor is created through WMI
                                (Win32_Process.Create), which always starts
                                outside the caller's job.
  process group + killpg        a per-run named Job Object. The supervisor
                                joins it before spawning the command, so the
                                command and every descendant are members;
                                kill opens it by name and TerminateJobObject
                                ends the whole tree. taskkill /T /F is the
                                fallback.
  kill(pid, 0) liveness         OpenProcess + GetExitCodeProcess (on Windows
                                os.kill(pid, 0) TERMINATES the process), with
                                the process creation time to reject a reused
                                PID.
  two ptys (line buffering)     two pipes pumped by threads (ConPTY gives a
                                process ONE console, which would merge
                                stdout and stderr and inject VT repaints).
  /bin/bash                     Git Bash (never System32\\bash.exe, which is
                                WSL), so command gating, .runner/scripts/*.sh
                                and runnerlog.sh behave exactly as on
                                macOS/Linux. RUNNER_SHELL overrides it
                                (bash | pwsh | powershell | cmd | a path).

Only imported on Windows. Standard library only (ctypes).
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Win32 API
# -----------------------------------------------------------------------------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5
JOB_OBJECT_TERMINATE = 0x0008
JOB_OBJECT_ASSIGN_PROCESS = 0x0001

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetExitCodeProcess.restype = wintypes.BOOL
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.GetProcessTimes.restype = wintypes.BOOL
_k32.GetCurrentProcess.argtypes = []
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
_k32.IsProcessInJob.restype = wintypes.BOOL
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k32.OpenJobObjectW.restype = wintypes.HANDLE
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.TerminateJobObject.restype = wintypes.BOOL


def _filetime_int(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def process_create_time(pid: int) -> int | None:
    """Creation time of `pid` as a FILETIME integer, or None if unavailable."""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return _filetime_int(c)
    finally:
        _k32.CloseHandle(h)


def process_alive(pid: int, created: int | None = None) -> bool:
    """True if `pid` is a live process (and, when `created` is given, the same
    process that was recorded -- Windows reuses PIDs quickly, and after a
    reboot a stale run's PID can belong to anything)."""
    if pid <= 0:
        return False
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # Access denied means it exists but belongs to someone else.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        if code.value != STILL_ACTIVE:
            return False
        if created:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return _filetime_int(c) == int(created)
        return True
    finally:
        _k32.CloseHandle(h)


def in_job() -> bool:
    flag = wintypes.BOOL(False)
    if not _k32.IsProcessInJob(_k32.GetCurrentProcess(), None, ctypes.byref(flag)):
        return False
    return bool(flag.value)


def create_own_job(name: str) -> int | None:
    """Create a named Job Object and put the CURRENT process in it, so every
    process it spawns afterwards (and their descendants) is a member. Returns
    the handle (kept open for the supervisor's lifetime) or None."""
    h = _k32.CreateJobObjectW(None, name)
    if not h:
        return None
    if not _k32.AssignProcessToJobObject(h, _k32.GetCurrentProcess()):
        _k32.CloseHandle(h)
        return None
    return h


def terminate_job(name: str, exit_code: int = 137) -> bool:
    h = _k32.OpenJobObjectW(JOB_OBJECT_TERMINATE, False, name)
    if not h:
        return False
    try:
        return bool(_k32.TerminateJobObject(h, exit_code))
    finally:
        _k32.CloseHandle(h)


def kill_tree(meta: dict[str, Any]) -> bool:
    """Kill a run's whole process tree: the Windows killpg.

    The run's Job Object holds the supervisor, the shell, and everything the
    command started, so terminating it is exact. taskkill /T (walks the
    parent-pid tree) covers a run whose job could not be created.
    """
    killed = False
    job = meta.get("jobName")
    if job:
        killed = terminate_job(job)
    for key in ("pid", "supervisorPid"):
        pid = int(meta.get(key) or 0)
        if pid > 0 and process_alive(pid):
            r = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            killed = killed or r.returncode == 0
    return killed


# -----------------------------------------------------------------------------
# Shell resolution
# -----------------------------------------------------------------------------

def _is_wsl_bash(p: str) -> bool:
    low = p.replace("/", "\\").lower()
    return "\\windows\\system32\\" in low or "\\windowsapps\\" in low


def find_git_bash() -> str | None:
    """Locate Git for Windows' bash.exe (never WSL's System32\\bash.exe)."""
    cands: list[Path] = []
    for env in ("RUNNER_GIT_BASH", "CLAUDE_CODE_GIT_BASH_PATH"):
        v = os.environ.get(env, "").strip()
        if v:
            cands.append(Path(v))
    git = shutil.which("git")
    if git:
        g = Path(git).resolve()
        # <root>\cmd\git.exe, <root>\bin\git.exe, <root>\mingw64\bin\git.exe
        for root in (g.parent.parent, g.parent.parent.parent):
            cands.append(root / "bin" / "bash.exe")
            cands.append(root / "usr" / "bin" / "bash.exe")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            cands.append(Path(base) / "Git" / "bin" / "bash.exe")
    which_bash = shutil.which("bash")
    if which_bash and not _is_wsl_bash(which_bash):
        cands.append(Path(which_bash))
    for c in cands:
        if c.is_file() and not _is_wsl_bash(str(c)):
            return str(c)
    return None


def resolve_shell() -> tuple[str, str]:
    """Return (kind, exe) for the shell that runs commands.

    kind is one of bash, pwsh, powershell, cmd. Default: Git Bash, then
    PowerShell 7, then Windows PowerShell. RUNNER_SHELL picks explicitly.
    """
    want = os.environ.get("RUNNER_SHELL", "").strip()
    if want:
        low = Path(want).name.lower().removesuffix(".exe")
        if low in ("bash", "git-bash", "sh"):
            exe = find_git_bash() if low != "bash" or not Path(want).is_file() else want
            if exe:
                return "bash", exe
        elif low in ("pwsh", "powershell", "cmd"):
            exe = want if Path(want).is_file() else shutil.which(low)
            if exe:
                return low, exe
    exe = find_git_bash()
    if exe:
        return "bash", exe
    for kind in ("pwsh", "powershell"):
        exe = shutil.which(kind)
        if exe:
            return kind, exe
    return "cmd", os.environ.get("ComSpec", "cmd.exe")


def shell_argv(kind: str, exe: str, cmd: str) -> list[str] | str:
    if kind == "bash":
        return [exe, "-c", cmd]
    if kind in ("pwsh", "powershell"):
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", cmd]
    # cmd.exe parses its own command line; hand it over verbatim.
    return f'"{exe}" /d /s /c "{cmd}"'


# -----------------------------------------------------------------------------
# Spawn (caller side)
# -----------------------------------------------------------------------------

FILE_SPAWN = "spawn.json"


def _wmi_create(cmdline: str, cwd: str) -> int | None:
    """Create a process through WMI (outside the caller's job). The command
    line travels in an environment variable so no quoting layer touches it."""
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return None
    script = (
        "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
        "-Property @{ShowWindow=[uint16]0};"
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine=$env:RUNNER_WMI_CMD; "
        "CurrentDirectory=$env:RUNNER_WMI_CWD; ProcessStartupInformation=$si};"
        "if ($r.ReturnValue -eq 0) { $r.ProcessId } else { exit 1 }"
    )
    env = dict(os.environ, RUNNER_WMI_CMD=cmdline, RUNNER_WMI_CWD=cwd)
    try:
        out = subprocess.run(
            [ps, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, env=env, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def launch_supervisor(core_path: Path, rdir: Path, cmd: str, cwd: str | None,
                      name: str, run_id: str) -> str:
    """Start the detached supervisor for one run. Returns the detach mode:

      "breakaway"  started outside the agent's job (normal case)
      "free"       the caller was not in a job; nothing to escape
      "wmi"        job forbade breakaway; created via WMI instead
      "job-bound"  every escape failed; the run dies with the agent's job
    """
    # The supervisor itself starts in the run dir, so pin the command's cwd
    # now: no cwd means the caller's, as with the POSIX fork.
    spec: dict[str, Any] = {"cmd": cmd, "cwd": cwd or os.getcwd(), "name": name, "runId": run_id}
    py = sys.executable
    argv = [py, str(core_path), "_supervise", "--run-dir", str(rdir)]
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    jobbed = in_job()

    def write_spec(with_env: bool) -> None:
        s = dict(spec)
        if with_env:
            # A WMI-created process gets the user's default environment, not
            # ours; hand ours over (the supervisor deletes it once read).
            s["env"] = dict(os.environ)
        (rdir / FILE_SPAWN).write_text(json.dumps(s), encoding="utf-8")

    write_spec(False)
    popen_kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True, cwd=str(rdir))
    try:
        subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **popen_kw)
        return "breakaway" if jobbed else "free"
    except OSError:
        pass
    write_spec(True)
    # Through cmd.exe: a Store-installed Python is an App Execution Alias,
    # which WMI cannot start directly (cmd resolves it as the user does).
    comspec = os.environ.get("ComSpec", "cmd.exe")
    wmi_line = f'"{comspec}" /d /s /c "{subprocess.list2cmdline(argv)}"'
    if _wmi_create(wmi_line, str(rdir)) is not None:
        return "wmi"
    write_spec(False)
    subprocess.Popen(argv, creationflags=flags, **popen_kw)
    return "job-bound"


# -----------------------------------------------------------------------------
# Supervisor (detached side)
# -----------------------------------------------------------------------------

def read_spec(rdir: Path) -> dict[str, Any]:
    p = rdir / FILE_SPAWN
    spec = json.loads(p.read_text(encoding="utf-8"))
    if "env" in spec:
        # Do not leave a copy of the environment on disk.
        slim = {k: v for k, v in spec.items() if k != "env"}
        p.write_text(json.dumps(slim), encoding="utf-8")
    return spec


def _pump(src, dst) -> None:
    fd = src.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            break
        try:
            dst.write(chunk)
            dst.flush()
        except Exception:
            pass
    try:
        src.close()
    except Exception:
        pass


def run_child(argv: list[str] | str, cwd: str | None, env: dict[str, str],
              stdout_path: Path, stderr_path: Path, on_started) -> int:
    """Run the command with both streams piped into the log files, live.
    `on_started(proc)` is called once the child exists. Returns the exit code.
    """
    out_log = open(str(stdout_path), "ab", buffering=0)
    err_log = open(str(stderr_path), "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or None,
            env=env,
            bufsize=0,
        )
    except OSError as e:
        err_log.write(f"runner: failed to start command: {e}\n".encode("utf-8"))
        out_log.close()
        err_log.close()
        return 127
    on_started(proc)
    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, out_log), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err_log), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rc = proc.wait()
    out_log.close()
    err_log.close()
    return rc
