#!/usr/bin/env python3
"""Split `codex exec --json` output into runner's two-log contract.

Codex writes JSONL events to stdout in --json mode and diagnostics to
stderr. Runner wants conversational text in stdout.log and the complete
event stream in stderr.log, so this filter mirrors every input line to
stderr and emits only completed agent messages to stdout.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    saw_event = False
    for raw in sys.stdin:
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        sys.stderr.write(raw + "\n")
        sys.stderr.flush()
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        saw_event = True
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.rstrip():
            sys.stdout.write(text.rstrip() + "\n")
            sys.stdout.flush()

    if not saw_event:
        sys.stdout.write(
            "[no events] codex produced no JSON output; "
            "see stderr.log for the raw cause.\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
