"""Windows detach harness: run `runner_core.py start` from inside a
kill-on-close Job Object (what agent hosts do to their children), then close
the job and report whether the run survived.

    python tests/win_detach_harness.py <breakaway|nobreakaway> <cmd...>

Prints the start response; the caller checks the run afterwards.
"""
import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateJobObjectW.restype = wintypes.HANDLE
k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]


class BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class IOC(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]


class EXT(ctypes.Structure):
    _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


KILL_ON_CLOSE = 0x2000
BREAKAWAY_OK = 0x800

mode = sys.argv[1]
cmd = " ".join(sys.argv[2:])
job = k32.CreateJobObjectW(None, None)
info = EXT()
info.Basic.LimitFlags = KILL_ON_CLOSE | (BREAKAWAY_OK if mode == "breakaway" else 0)
assert k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
assert k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())

core = Path(__file__).resolve().parent.parent / "core" / "runner_core.py"
out = subprocess.run([sys.executable, str(core), "start", "--cmd", cmd, "--no-blocking",
                      "--no-scrub", "--name", f"detach-{mode}"], capture_output=True, text=True)
print(out.stdout.strip() or out.stderr.strip())
sys.stdout.flush()
# Closing the last handle kills every process still in the job -- including
# this one, which is the point: only a run that escaped survives.
k32.CloseHandle(job)
