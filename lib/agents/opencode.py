"""
opencode -- agent runtime helper.

Three responsibilities, mirroring the rest of the multi-agent surface:

  build_cmd(prompt_path, session_id) -> str
      Returns the shell command runner should spawn for one turn. The command
      pipes opencode's `--format json` NDJSON through the renderer, so that
      runner's stdout.log holds the rendered transcript and stderr.log holds
      the raw NDJSON event stream.

  extract(rdir) -> dict
      Scans the run dir's stderr.log (raw NDJSON) and returns a small summary
      dict including the opencode sessionID. Called lazily by runner on every
      status/list read for any run with meta.agentRuntime == "opencode"; the
      first successful extraction is persisted into meta.json.

  compact_view(rdir) -> dict
      Synthesizes the agent-facing response shape from the NDJSON event
      stream. Returns a small dict containing the sub-agent's text replies
      plus one-line summaries of tool calls -- NOT the full rendered
      transcript (that lives on disk for runner_grep / runner_section
      drill-down). Also produces a `currentActivity` block describing the
      most recent tool so the caller can see what an in-flight turn is
      doing. Each backend (opencode, claude, ...) needs its own version of
      this since the event shape is backend-specific.

The helper does NOT touch meta.json itself -- runner_core owns that. We
just return what we found.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Resolved at import time. Renderer sits next to this file.
_RENDER_SCRIPT = Path(__file__).resolve().parent / "opencode_render.py"


def render_script_path() -> Path:
    return _RENDER_SCRIPT


def _current_turn_byte_bounds(rdir: Path, which: str) -> tuple[int, int]:
    """Look up the current-turn byte range on stdout or stderr.

    Reads meta.json on demand (cheap, JSON of a few hundred bytes) and
    delegates to lib.agents.turn_bounds. Returns (0, sys.maxsize) when
    meta is missing or has no agentTurnCursors -- callers then read
    the full file (legacy / fresh-run safe fallback).
    """
    import sys as _sys
    meta_path = rdir / "meta.json"
    if not meta_path.exists():
        return (0, _sys.maxsize)
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return (0, _sys.maxsize)
    # Lazy import to avoid a circular module load -- this file is loaded
    # by agents/__init__.py at registry build time.
    from . import turn_bounds  # type: ignore[attr-defined]
    return turn_bounds(meta, which)


def build_cmd(prompt_path: Path, session_id: str | None) -> str:
    """Build the shell command runner should exec for one turn.

    Pipeline:
        opencode run --format json [-s SID] < prompt.md | python3 <renderer>

    - prompt.md is fed via stdin so multi-line prompts with special chars
      don't need shell escaping.
    - opencode's own stderr is NOT redirected. In a bash pipeline, only
      stdout is piped between processes; both opencode and the renderer
      inherit the parent's stderr. _spawn_into points that at
      stderr.log, so opencode's error messages (e.g. "Session not
      found: ses_..." when -s targets a missing session) land in
      stderr.log alongside the renderer's raw NDJSON mirror. They are
      shape-distinct (NDJSON starts with '{'; opencode errors are
      free-form text starting with names like NotFoundError) so an
      agent reading the response can tell them apart at a glance.
      We intentionally do NOT classify or surface these errors in the
      response: the agent will see them in the transcript and respond
      appropriately.
    - The renderer's stdout -> runner's stdout.log (rendered text).
      The renderer's stderr -> runner's stderr.log (raw NDJSON +
      whatever opencode wrote to stderr).
    """
    session_flag = f"-s {_sh_quote(session_id)} " if session_id else ""
    return (
        f"opencode run --format json {session_flag}"
        f"< {_sh_quote(str(prompt_path))} "
        f"| python3 {_sh_quote(str(_RENDER_SCRIPT))}"
    )


def extract(rdir: Path) -> dict[str, Any]:
    """Extract session info from the run dir's stderr.log (raw NDJSON).

    stderr.log contains both the renderer's mirrored NDJSON events AND
    any free-form error text opencode itself wrote (since we don't
    redirect opencode's stderr -- see build_cmd notes). The two are
    shape-distinct so we just skip anything that isn't valid JSON.

    Returns at minimum:
      {sessionId: str | None, lastTokens: int | None,
       lastReason: str | None, toolCallCount: int}

    Designed to be safe to call repeatedly; only reads, never writes.
    """
    stderr_path = rdir / "stderr.log"
    out: dict[str, Any] = {
        "sessionId": None,
        "lastTokens": None,
        "lastReason": None,
        "toolCallCount": 0,
    }
    if not stderr_path.exists():
        return out
    try:
        with stderr_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    # Opencode's own error output (free-form text). Skipped
                    # here -- the agent reads it directly out of stderr.log.
                    continue
                sid = ev.get("sessionID")
                if sid and out["sessionId"] is None:
                    out["sessionId"] = sid
                kind = ev.get("type", "")
                part = ev.get("part") or {}
                ptype = part.get("type", "")
                if kind == "tool_use" or ptype == "tool":
                    out["toolCallCount"] += 1
                if kind == "step_finish" or ptype == "step-finish":
                    toks = part.get("tokens") or {}
                    if toks.get("total") is not None:
                        out["lastTokens"] = toks["total"]
                    if part.get("reason"):
                        out["lastReason"] = part["reason"]
    except OSError:
        pass
    return out


def _parse_events(rdir: Path) -> list[dict[str, Any]]:
    """One-pass parser over the CURRENT TURN's slice of stderr.log NDJSON.

    Sub-agent runs use append-only stderr.log across the entire
    conversation, so we seek to the byte offset where the current turn
    started (recorded in meta.agentTurnCursors) and parse from there to
    EOF. For legacy/non-agent runs without cursors, the helper falls
    back to reading the whole file.

    Each returned event is one of:
      {"kind": "session_id", "value": "ses_..."}            # synthetic, first only
      {"kind": "step_start"}
      {"kind": "step_finish", "reason": str, "tokens": int|None}
      {"kind": "text", "text": str}
      {"kind": "tool",
         "name": str, "description": str|None, "status": str,
         "outputLines": int|None, "startedAtMs": int|None}
      {"kind": "backend_error", "text": str}                # soft error
      {"kind": "backend_event_error",
         "errorClass": str, "errorKind": str|None, "message": str}

    The agent never sees this list directly -- final_reply() and
    live_progress() consume it and shape the user-facing response.

    By centralizing parsing here, the JSON event format is interpreted in
    exactly ONE place; the view-builders just walk a clean list.
    """
    import json as _json_mod  # local re-bind, parser-internal
    out: list[dict[str, Any]] = []
    stderr_path = rdir / "stderr.log"
    if not stderr_path.exists():
        return out
    # Look up the current-turn byte range. For the current turn, byte_end
    # is effectively +inf (we read to EOF), so we only need to seek to
    # the start. cmd_agent always kills the prior turn before spawning a
    # new one, so there's never a case where a NEWER turn's bytes have
    # already been appended while we're parsing the current turn.
    byte_start, _ = _current_turn_byte_bounds(rdir, "stderr")
    seen_sid = False
    try:
        with stderr_path.open("r", encoding="utf-8", errors="replace") as f:
            if byte_start > 0:
                f.seek(byte_start)
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    # opencode's own stderr (not NDJSON). Capture distinct
                    # error-line shapes so the agent can see WHY something
                    # failed. Two categories:
                    #   - "soft" errors (Error: / error: / message: text):
                    #       captured for context; not by themselves enough
                    #       to declare the turn interrupted.
                    #   - "hard" errors (rate limit / HTTP error codes /
                    #     auth-proxy failures): the sub-agent's request to
                    #     its LLM provider failed. opencode usually exits
                    #     soon after, but sometimes hangs retrying. We
                    #     classify these as backend_event_error too (same
                    #     kind as structured type:error events) so the
                    #     interrupt path fires without waiting for the
                    #     wrapping process to die.
                    # Skip JS-style stack-trace lines as noise.
                    if raw.startswith("at ") or raw.startswith("}"):
                        continue
                    text_lc = raw.lower()
                    is_hard_error = (
                        "rate limit" in text_lc
                        or "ratelimit" in text_lc
                        or "api 429" in text_lc
                        or "api 500" in text_lc
                        or "api 502" in text_lc
                        or "api 503" in text_lc
                        or "overloaded" in text_lc
                    )
                    if is_hard_error:
                        # Try to extract a concise reason ("Rate limited",
                        # "Overloaded", etc.) for the response shape.
                        if "rate limit" in text_lc or "429" in raw:
                            reason, kind_hint = "Rate limited", "rate_limit_error"
                        elif "overloaded" in text_lc:
                            reason, kind_hint = "Overloaded", "overloaded_error"
                        else:
                            reason, kind_hint = raw[:120], "backend_error"
                        out.append({
                            "kind": "backend_event_error",
                            "errorClass": "BackendStderr",
                            "errorKind": kind_hint,
                            "message": reason,
                        })
                        continue
                    if "Error:" in raw or "error:" in raw or "message:" in raw:
                        out.append({"kind": "backend_error", "text": raw[:240]})
                    continue
                sid = ev.get("sessionID")
                if sid and not seen_sid:
                    out.append({"kind": "session_id", "value": sid})
                    seen_sid = True
                kind = ev.get("type", "")
                part = ev.get("part") or {}
                ptype = part.get("type", "")
                if kind == "step_start" or ptype == "step-start":
                    out.append({"kind": "step_start"})
                elif kind == "step_finish" or ptype == "step-finish":
                    toks = part.get("tokens") or {}
                    out.append({
                        "kind": "step_finish",
                        "reason": part.get("reason", ""),
                        "tokens": toks.get("total"),
                    })
                elif kind == "text" or ptype == "text":
                    text = (part.get("text") or "").rstrip()
                    if text:
                        out.append({"kind": "text", "text": text})
                elif kind == "tool_use" or ptype == "tool":
                    state = part.get("state") or {}
                    input_obj = state.get("input") or {}
                    metadata = state.get("metadata") or {}
                    description = (
                        part.get("title")
                        or metadata.get("description")
                        or input_obj.get("description")
                        or _summarize_tool_input(part.get("tool") or "", input_obj)
                        or None
                    )
                    out.append({
                        "kind": "tool",
                        "name": part.get("tool") or "?",
                        "description": description,
                        "status": state.get("status", "?"),
                        "outputLines": _output_line_count(state.get("output")),
                        "startedAtMs": (state.get("time") or {}).get("start"),
                    })
                elif kind == "error":
                    # opencode emits a structured error event when an LLM
                    # request fails (rate limit, API down, etc.). The
                    # process exits 0 anyway -- without parsing this event
                    # we'd report the turn as "successfully finished but
                    # somehow has no reply", and the caller would poll-bomb
                    # trying to figure out why.
                    err = ev.get("error") or {}
                    data = err.get("data") or {}
                    code = err.get("name") or "?"   # e.g. "APIError"
                    msg = data.get("message") or err.get("message") or ""
                    # Try to narrow the kind via responseBody (anthropic's
                    # rate_limit_error / overloaded_error / etc.) so the
                    # caller can choose how to react.
                    body = data.get("responseBody") or ""
                    kind_hint: str | None = None
                    if body:
                        try:
                            parsed_body = json.loads(body) if isinstance(body, str) else body
                            kind_hint = (parsed_body or {}).get("type")
                        except json.JSONDecodeError:
                            pass
                    if not kind_hint and "rate" in msg.lower():
                        kind_hint = "rate_limit_error"
                    out.append({
                        "kind": "backend_event_error",
                        "errorClass": code,
                        "errorKind": kind_hint,
                        "message": msg or code,
                    })
    except OSError:
        pass
    return out


def _terminal_step_bounds(events: list[dict[str, Any]]) -> tuple[int, int] | None:
    """If there's a step ending with reason=stop, return (step_start_index,
    step_finish_index). Otherwise None.

    A "step" in opencode's stream is a single LLM call. A "turn" (one
    opencode run invocation) typically has many steps interleaved with
    tool calls; only the LAST step ends with reason=stop, and its text
    parts are the sub-agent's final answer.
    """
    stop_idx: int | None = None
    for i, ev in enumerate(events):
        if ev["kind"] == "step_finish" and ev.get("reason") == "stop":
            stop_idx = i  # take the LAST one in case opencode ever emits multiple
    if stop_idx is None:
        return None
    # Walk backwards to the matching step_start
    for j in range(stop_idx - 1, -1, -1):
        if events[j]["kind"] == "step_start":
            return (j, stop_idx)
    # No preceding step_start (malformed); treat the stop event itself as
    # the only bound -- text just before it is still the reply.
    return (0, stop_idx)


def _tool_one_liner(ev: dict[str, Any]) -> str:
    """Render a tool event as a single line for recent-tool lists.

    Format: `[tool: NAME] description -> N lines (status)`
    Mirrors the renderer's pattern so agents see consistent formatting
    whether they're reading recentToolCalls or grepping stdout.log.
    """
    desc_part = f" -- {ev['description']}" if ev.get("description") else ""
    size_part = f" -> {ev['outputLines']} lines" if ev.get("outputLines") is not None else ""
    return f"[tool: {ev['name']}]{desc_part}{size_part} ({ev['status']})"


def final_reply(rdir: Path) -> dict[str, Any] | None:
    """If the turn has completed (reason=stop seen), return the agent's
    final answer in a focused shape. Otherwise None.

    Shape:
      {
        "text": str,                         # the sub-agent's final reply, joined
        "totalToolCalls": int,               # tool calls across the whole turn
        "totalTokens": int | None,           # token total at reason=stop
        "recentToolCalls": list[str],        # up to 8 most recent, for context
        "backendErrors": list[str] | None,   # any captured opencode error lines
      }

    The text is bounded by what the sub-agent actually said -- typically
    small even on marathon turns (5-10 KB) since text is the conclusion,
    not the work. Tool calls collapse to a count + recent tail.
    """
    events = _parse_events(rdir)
    bounds = _terminal_step_bounds(events)
    if bounds is None:
        return None
    start_idx, stop_idx = bounds
    # Collect text parts within the final step
    text_parts: list[str] = []
    for k in range(start_idx, stop_idx + 1):
        if events[k]["kind"] == "text":
            text_parts.append(events[k]["text"])
    # Totals across the whole turn
    total_tools = sum(1 for ev in events if ev["kind"] == "tool")
    total_tokens = events[stop_idx].get("tokens")
    # Last 8 tool-call summaries (across the whole turn, in order)
    tool_evs = [ev for ev in events if ev["kind"] == "tool"]
    recent = [_tool_one_liner(ev) for ev in tool_evs[-8:]]
    backend_errs = [ev["text"] for ev in events if ev["kind"] == "backend_error"]
    return {
        "text": "\n\n".join(t for t in text_parts if t).strip(),
        "totalToolCalls": total_tools,
        "totalTokens": total_tokens,
        "recentToolCalls": recent,
        "backendErrors": backend_errs or None,
    }


def live_progress(rdir: Path, *, started_at: int | None = None) -> dict[str, Any]:
    """Return a small "what's happening now" view for an in-flight turn.

    Shape (any field may be absent when no signal exists):
      {
        "currentActivity": {tool, description, status, startedAtMs},
        "tokensSoFar": int | None,
        "lastReason": str | None,           # "tool-calls" mid-turn, "stop" if done
        "toolCallCount": int,
        "recentToolCalls": list[str],        # last 5
        "turnDurationSec": int | None,
        "backendErrors": list[str] | None,
      }

    Safe to call repeatedly. Reads only; no side effects.
    """
    import time
    events = _parse_events(rdir)
    tool_evs = [ev for ev in events if ev["kind"] == "tool"]
    tokens_so_far: int | None = None
    last_reason: str | None = None
    for ev in events:
        if ev["kind"] == "step_finish":
            if ev.get("tokens") is not None:
                tokens_so_far = ev["tokens"]
            if ev.get("reason"):
                last_reason = ev["reason"]
    current: dict[str, Any] | None = None
    if tool_evs:
        last = tool_evs[-1]
        current = {
            "tool": last["name"],
            "description": last.get("description"),
            "status": last["status"],
            "startedAtMs": last.get("startedAtMs"),
        }
    backend_errs = [ev["text"] for ev in events if ev["kind"] == "backend_error"]
    return {
        "currentActivity": current,
        "tokensSoFar": tokens_so_far,
        "lastReason": last_reason,
        "toolCallCount": len(tool_evs),
        "recentToolCalls": [_tool_one_liner(ev) for ev in tool_evs[-5:]],
        "turnDurationSec": (int(time.time()) - started_at) if started_at else None,
        "backendErrors": backend_errs or None,
    }


def _interrupted(rdir: Path) -> dict[str, Any] | None:
    """Detect a recoverable interruption.

    Returns a dict when the most recent NDJSON event is a backend-emitted
    error (e.g. opencode's `type: "error"` event for an API rate-limit /
    overload / network failure). The sub-agent's process exits 0 anyway
    so meta.state goes to "exited" -- without surfacing this, the caller
    would see a "terminal but no reply" response and poll-bomb trying to
    figure out why.

    Returns None when there's no terminal error or when a reason=stop
    step exists (i.e. the turn completed successfully and any errors
    along the way were recoverable on the sub-agent's side).

    Shape when present:
      {
        "reason":  str,    # short human-readable cause ("Rate limited")
        "code":    str,    # structured class ("APIError", "ToolError", ...)
        "kind":    str | None,  # narrow kind from responseBody when present
                                # ("rate_limit_error", "overloaded_error", ...)
      }
    """
    events = _parse_events(rdir)
    # If the turn reached a clean stop, any earlier errors don't count
    # as terminal interruption -- the sub-agent recovered.
    if _terminal_step_bounds(events) is not None:
        return None
    # Look for an error event near the tail. We accept any error event
    # in the sequence as evidence that the turn was interrupted, but
    # prefer the LAST one if there are several.
    last_err: dict[str, Any] | None = None
    for ev in events:
        if ev["kind"] == "backend_event_error":
            last_err = ev
    if last_err is None:
        return None
    return {
        "reason": last_err.get("message") or last_err.get("errorClass") or "unknown",
        "code": last_err.get("errorClass") or "?",
        "kind": last_err.get("errorKind"),
    }


def compact_view(rdir: Path, *, terminal: bool, started_at: int | None = None) -> dict[str, Any]:
    """Registry-contract entry point. Returns the right view based on
    what's actually in the NDJSON -- terminal completion, in-flight
    progress, or recoverable interruption.

    Returns one of three views (mutually exclusive at top level):
      {
        "finalReply":  {...} | None,  # turn completed; reason=stop was emitted
        "progress":    {...} | None,  # in-flight; no terminal signal yet
        "interrupted": {...} | None,  # turn ended without reason=stop AND
                                      # NDJSON has a backend error event
                                      # (rate limit, API error). Caller
                                      # should dispatch a 'continue' prompt
                                      # on the same runId to resume.
        # Common summary fields lifted regardless of state:
        "tokensSoFar":     int | None,
        "lastReason":      str | None,
        "toolCallCount":   int,
        "turnDurationSec": int | None,
      }

    Precedence: finalReply > interrupted > progress. (A turn with both
    text AND a later error is treated as completed -- text is what the
    caller wanted.)
    """
    pg = live_progress(rdir, started_at=started_at)
    common = {
        "tokensSoFar": pg["tokensSoFar"],
        "lastReason": pg["lastReason"],
        "toolCallCount": pg["toolCallCount"],
        "turnDurationSec": pg["turnDurationSec"],
    }
    fr = final_reply(rdir)
    if fr is not None:
        return {"finalReply": fr, "progress": None, "interrupted": None, **common}
    interrupted = _interrupted(rdir)
    if interrupted is not None and terminal:
        # Only surface "interrupted" when the wrapping process has
        # actually terminated. Mid-flight error events that the sub-agent
        # might recover from on its own are left to the progress view.
        return {"finalReply": None, "progress": None, "interrupted": interrupted, **common}
    return {"finalReply": None, "progress": pg, "interrupted": None, **common}


def _summarize_tool_input(tool_name: str, input_obj: dict[str, Any]) -> str:
    """Fallback one-liner for tools that didn't carry a description.

    Picks the most informative-looking field for a few well-known tools.
    Returns "" when nothing useful is available -- caller treats that as
    "no description".
    """
    if not isinstance(input_obj, dict):
        return ""
    # Generic preference order: a short identifying field
    for key in ("selector", "pattern", "command", "filePath", "path", "url"):
        v = input_obj.get(key)
        if isinstance(v, str) and v:
            # Trim long values so the line stays scannable
            return f"{key}={v[:80]}" if len(v) <= 80 else f"{key}={v[:80]}..."
    return ""


def _output_line_count(output: Any) -> int | None:
    """Return the number of newline-separated lines in a tool's output.

    Used to tell the caller how much was produced without dumping any of
    it into the response. None means we can't count (non-string output).
    """
    if output is None:
        return None
    if isinstance(output, str):
        if not output:
            return 0
        return output.count("\n") + (0 if output.endswith("\n") else 1)
    return None


def _sh_quote(s: str) -> str:
    """Single-quote a string for safe bash inclusion."""
    return "'" + s.replace("'", "'\\''") + "'"
