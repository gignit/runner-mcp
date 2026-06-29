"""
runnerlog -- python module for the runner protocol.

Usage (function API):
    import sys, os
    sys.path.insert(0, os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "runner-mcp", "lib"))
    import runnerlog

    runnerlog.section_start("compile")
    # ... do work ...
    runnerlog.section_end("compile", status="ok", exit=0)

    runnerlog.metric(files=42, bytes=12345)
    runnerlog.event("doc 1 of 19 complete")
    runnerlog.fail("config missing")          # exits the script

Usage (context manager -- recommended for Python because it auto-closes
the section even on exception):

    with runnerlog.section("compile"):
        # ... do work ...
        # If this raises, the section is closed with status="failed" and
        # the exception's class name as the reason.

Direct JSON serialization (no subprocess): faster than the bash shim for
hot paths emitting hundreds of metrics.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import traceback
from typing import Any, Iterator

WIRE_PREFIX = "::run:: "


def _emit(payload: dict[str, Any]) -> None:
    """Write one protocol line to stdout, with timestamp + flush."""
    payload.setdefault("ts", int(time.time()))
    sys.stdout.write(WIRE_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def section_start(name: str, **fields: Any) -> None:
    _emit({"v": "section_start", "name": name, **fields})


def section_end(name: str, status: str = "ok", *, exit: int | None = None,
                reason: str | None = None, **fields: Any) -> None:
    payload: dict[str, Any] = {"v": "section_end", "name": name, "status": status, **fields}
    if exit is not None:
        payload["exit"] = int(exit)
    if reason is not None:
        payload["reason"] = str(reason)
    _emit(payload)


def metric(**fields: Any) -> None:
    if not fields:
        return
    _emit({"v": "metric", **fields})


def event(msg: str, **fields: Any) -> None:
    _emit({"v": "event", "msg": msg, **fields})


def fail(msg: str) -> None:
    _emit({"v": "fail", "msg": msg})
    sys.exit(1)


@contextlib.contextmanager
def section(name: str, **start_fields: Any) -> Iterator[None]:
    """Context manager: opens a section, closes it on exit.

    On normal exit -> status=ok exit=0
    On exception   -> status=failed exit=1 reason=<exception class>
                      AND re-raises the exception
    """
    section_start(name, **start_fields)
    try:
        yield
    except SystemExit as e:
        # Honor explicit exit codes
        code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
        if code == 0:
            section_end(name, status="ok", exit=0)
        else:
            section_end(name, status="failed", exit=code, reason="SystemExit")
        raise
    except BaseException as e:
        section_end(
            name,
            status="failed",
            exit=1,
            reason=f"{type(e).__name__}: {e}",
        )
        raise
    else:
        section_end(name, status="ok", exit=0)
