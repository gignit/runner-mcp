#!/usr/bin/env python3
"""
opencode_render -- conversation filter for opencode NDJSON.

Pipeline position:

    opencode run --format json [-s SID] < prompt.md | python3 opencode_render.py

Outputs:

  stdout (-> runner's stdout.log):
      Only the SUB-AGENT'S CONVERSATIONAL TEXT -- the things it
      "says" in reply to the prompt. One newline-separated text
      block per `text` event from opencode. Nothing else. No tool
      calls, no step boundaries, no session header, no metadata.
      The intent: stdout reads like a human conversation, top to
      bottom, without scaffolding noise.

  stderr (-> runner's stderr.log):
      The COMPLETE raw NDJSON event stream mirrored verbatim,
      one event per line. This is the source of truth for
      everything else: compact_view / final_reply /
      live_progress / _interrupted all read structured events
      from here. Agents can also runner_grep / runner_section
      on this file to drill into any specific tool call, input,
      output, or metric.

Why the split:
  - Tool call inputs and outputs are bulk data (multi-KB bash
    commands, file dumps, huge git commit messages). They belong
    only in the structured log where they can be searched
    deterministically. Inlining them into a "rendered transcript"
    produced an unreadable wall of text and made the human-
    facing stdout useless.
  - The agent's conclusions / reasoning / dialogue (the `text`
    parts) ARE what a downstream agent or human wants to read
    top-to-bottom. Those are kept in stdout.

This file does no parsing of tool calls / metadata. It is a
strict filter: text in => text out, everything else => stderr.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    saw_any_event = False
    for raw in sys.stdin:
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        # Always mirror the raw line to stderr first, regardless of
        # whether it parses. opencode's own stderr also flows to the
        # parent stderr (inherited fd), so plain-text errors from
        # opencode interleave here with NDJSON events -- which is
        # exactly what the interrupt-detection code expects to read.
        sys.stderr.write(raw + "\n")
        sys.stderr.flush()
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            # Non-JSON line -- already mirrored to stderr above; don't
            # pollute stdout with it. (compact_view / _interrupted will
            # pick it up from stderr.log.)
            continue
        saw_any_event = True
        kind = ev.get("type", "")
        part = ev.get("part") or {}
        ptype = part.get("type", "")
        # Stdout gets ONLY conversational text parts.
        if kind == "text" or ptype == "text":
            text = (part.get("text") or "").rstrip()
            if text:
                sys.stdout.write(text + "\n")
                sys.stdout.flush()
        # Every other event type (step_start, step_finish, tool_use,
        # anything new opencode adds in the future) is silently
        # consumed here. It's still in stderr.log via the raw mirror
        # above, where structured tooling reads it.

    if not saw_any_event:
        # No NDJSON at all -- almost always means opencode itself
        # failed to start (e.g. bad -s session id; the error text is
        # in opencode's own stderr, already captured to stderr.log
        # via fd inheritance). Emit a one-line pointer so stdout
        # isn't empty when the caller looks at it.
        sys.stdout.write(
            "[no events] opencode produced no JSON output; "
            "see stderr.log for the raw cause.\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
