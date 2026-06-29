"""
claude -- agent runtime helper (Claude Code via the Agent SDK).

Implements the same three-function registry contract as lib/agents/opencode
(see agents/__init__.py): build_cmd / extract / compact_view. One turn is
one spawn of lib/agents/claude_runner.mjs, which drives the
@anthropic-ai/claude-agent-sdk and writes:

    stdout.log  -- the sub-agent's conversational text (assistant text blocks)
    stderr.log  -- the complete SDK message stream as NDJSON, one message
                   per line, plus the claude subprocess's free-form stderr

SDK message shapes we consume (all carry `session_id`):

    {type: "system",    subtype: "init", ...}
    {type: "assistant", message: {content: [{type:"text"|"tool_use",...}],
                                  usage: {...}}}
    {type: "user",      message: {content: [{type:"tool_result",
                                              tool_use_id, content, is_error}]}}
    {type: "result",    subtype: "success" | "error_*", result?, usage,
                        num_turns, is_error}
    {type: "runner_error", message}   # synthetic, from claude_runner.mjs

A turn is complete when a `result` message with subtype "success" exists;
a terminal run without one is classified as interrupted (mirroring
opencode's reason=stop logic).

Requires `claude` on PATH at runtime (claude_runner.mjs enforces this),
exactly as the opencode backend requires `opencode`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Shared plumbing with the opencode backend -- same package, same shapes.
from .opencode import (
    _current_turn_byte_bounds,
    _output_line_count,
    _sh_quote,
    _summarize_tool_input,
    _tool_one_liner,
)

# Resolved at import time. Runner script sits next to this file.
_RUNNER_SCRIPT = Path(__file__).resolve().parent / "claude_runner.mjs"

# Claude session ids are plain UUIDs (any version: normally v4, but callers
# can pre-assign arbitrary UUIDs via `claude --session-id`, including
# v7-shaped ones that collide with runner runId shape).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def looks_like_session_id(s: str) -> bool:
    """Shape check only -- use find_session() to verify existence."""
    return bool(_UUID_RE.match(s or ""))


def find_session(session_id: str, cwd: str | None = None) -> dict[str, Any] | None:
    """Locate a Claude Code session on disk and report which project owns it.

    Claude Code stores transcripts at
    ~/.claude/projects/<munged-project-dir>/<session-id>.jsonl and
    `--resume` only finds a session when the process cwd is that
    project. The munging is lossy ('/', '.', '_' all become '-'), so we
    don't decode the directory name -- each JSONL record carries the
    real `cwd`, and we read it from the file instead.

    Returns None when no transcript exists. Otherwise:
      {
        "projectDir":  str | None,   # real cwd to spawn claude in
        "sameProject": bool,         # projectDir == caller's cwd
        "transcript":  str,          # the .jsonl path
      }
    projectDir is None when the transcript has no cwd field (never seen
    in practice); callers should then fall back to their own cwd.
    """
    if not looks_like_session_id(session_id):
        return None
    projects = Path.home() / ".claude" / "projects"
    try:
        matches = sorted(projects.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    if not matches:
        return None
    caller = str(Path(cwd).resolve()) if cwd else None

    def _session_cwd(transcript: Path) -> str | None:
        try:
            with transcript.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i >= 50:
                        break
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    c = rec.get("cwd")
                    if isinstance(c, str) and c:
                        return c
        except OSError:
            pass
        return None

    # A session id existing under two projects is near-impossible, but if
    # it happens prefer the one owned by the caller's project.
    best = matches[0]
    best_cwd = _session_cwd(best)
    for m in matches[1:]:
        c = _session_cwd(m)
        if caller and c and str(Path(c).resolve()) == caller:
            best, best_cwd = m, c
            break
    same = bool(
        caller and best_cwd and str(Path(best_cwd).resolve()) == caller
    )
    return {
        "projectDir": best_cwd,
        "sameProject": same,
        "transcript": str(best),
    }


def runner_script_path() -> Path:
    return _RUNNER_SCRIPT


# Shown by the `models` CLI command next to this backend's list. The SDK's
# supportedModels() is ADVISORY, not exhaustive: model selection is passed
# through to Claude Code unvalidated, so ids the SDK doesn't report --
# e.g. claude-fable-5 -- still work.
MODELS_NOTE = (
    "Advisory list from the Agent SDK's supportedModels(). Not exhaustive: "
    "any model id Claude Code accepts works even if not listed here "
    "(e.g. claude-fable-5); it is passed through unvalidated."
)


def available_models() -> list[dict[str, Any]]:
    """Models reported by the Agent SDK, for the `models` CLI command.

    Shells out to `node claude_runner.mjs --list-models`, which asks a
    promptless SDK session for supportedModels() and prints a JSON array
    of {value, displayName, description}. Returns
    [{"model": value, "description": "displayName -- description"}, ...];
    empty when node/claude is missing or the SDK errors (callers treat
    that as "no models found", same as the opencode backend).
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["node", str(_RUNNER_SCRIPT), "--list-models"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        raw = json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for m in raw:
        if not isinstance(m, dict) or not m.get("value"):
            continue
        desc = " -- ".join(
            s for s in (m.get("displayName"), m.get("description")) if s
        )
        entry: dict[str, Any] = {"model": m["value"]}
        if desc:
            entry["description"] = desc
        out.append(entry)
    return out


def validate_model(model: str) -> tuple[bool, list[str]]:
    """Sanity-check a model override for the claude backend.

    Claude Code accepts aliases (sonnet, opus, haiku) or full model ids
    (claude-sonnet-5, claude-opus-4-8, ...) -- there is no cheap local
    catalog to enumerate, so anything plausible passes through and an
    actually-unknown model surfaces as an API error in the turn. The one
    shape we reject up front is opencode's `provider/model` form, since
    that's the likeliest cross-backend mistake.
    """
    hints = [
        "sonnet", "opus", "haiku",
        "(or a full model id like claude-sonnet-5)",
    ]
    if not model or "/" in model:
        return (False, hints)
    return (True, hints)


def build_cmd(prompt_path: Path, session_id: str | None, model: str | None = None) -> str:
    """Build the shell command runner should exec for one turn.

        node <claude_runner.mjs> --prompt-file prompt.md [--session SID] [--model M]

    The runner script does the stdout/stderr split itself (no pipe), so
    stdout.log gets the rendered conversation and stderr.log the raw
    NDJSON -- same contract as the opencode pipeline.
    """
    session_flag = f" --session {_sh_quote(session_id)}" if session_id else ""
    model_flag = f" --model {_sh_quote(model)}" if model else ""
    return (
        f"node {_sh_quote(str(_RUNNER_SCRIPT))} "
        f"--prompt-file {_sh_quote(str(prompt_path))}"
        f"{session_flag}{model_flag}"
    )


def extract(rdir: Path) -> dict[str, Any]:
    """Extract session info from the run dir's stderr.log (raw NDJSON).

    Returns at minimum:
      {sessionId: str | None, lastTokens: int | None,
       lastReason: str | None, toolCallCount: int}

    Scans the WHOLE file (not just the current turn) since the first
    session_id may predate the current turn. Safe to call repeatedly.
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
                    continue
                sid = ev.get("session_id")
                if sid and out["sessionId"] is None:
                    out["sessionId"] = sid
                kind = ev.get("type", "")
                if kind == "assistant":
                    content = ((ev.get("message") or {}).get("content")) or []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            out["toolCallCount"] += 1
                    toks = _usage_total(((ev.get("message") or {}).get("usage")))
                    if toks is not None:
                        out["lastTokens"] = toks
                elif kind == "result":
                    toks = _usage_total(ev.get("usage"))
                    if toks is not None:
                        out["lastTokens"] = toks
                    out["lastReason"] = ev.get("subtype") or None
    except OSError:
        pass
    return out


def _usage_total(usage: Any) -> int | None:
    """Context-size total from an Anthropic usage block: input + cache
    reads/writes + output. None when the block is missing/malformed."""
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for key in ("input_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens", "output_tokens"):
        v = usage.get(key)
        if isinstance(v, (int, float)):
            total += int(v)
            seen = True
    return total if seen else None


def _parse_events(rdir: Path) -> list[dict[str, Any]]:
    """One-pass parser over the CURRENT TURN's slice of stderr.log NDJSON.

    Produces the same normalized event list shapes as
    opencode._parse_events so the view-builders below stay parallel:

      {"kind": "session_id", "value": "..."}                  # first only
      {"kind": "text", "text": str}
      {"kind": "tool", "name", "description", "status",
                        "outputLines", "startedAtMs"}
      {"kind": "result", "reason": str, "tokens": int|None,
                          "text": str, "isError": bool}
      {"kind": "backend_error", "text": str}                  # soft error
      {"kind": "backend_event_error",
         "errorClass": str, "errorKind": str|None, "message": str}

    Tool events are created on tool_use and mutated in place when the
    matching tool_result arrives (status/outputLines), keyed by the
    Anthropic tool_use id.
    """
    out: list[dict[str, Any]] = []
    stderr_path = rdir / "stderr.log"
    if not stderr_path.exists():
        return out
    byte_start, _ = _current_turn_byte_bounds(rdir, "stderr")
    seen_sid = False
    tool_by_id: dict[str, dict[str, Any]] = {}
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
                    # The claude subprocess's own stderr (free-form text).
                    # Same classification approach as the opencode parser:
                    # hard API failures fire the interrupt path, other
                    # error-looking lines are captured as soft context.
                    text_lc = raw.lower()
                    is_hard_error = (
                        "rate limit" in text_lc
                        or "ratelimit" in text_lc
                        or "overloaded" in text_lc
                        or "credit balance" in text_lc
                        or "401" in raw or "429" in raw
                        or "api error" in text_lc
                    )
                    if is_hard_error:
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
                    if "Error:" in raw or "error:" in raw:
                        out.append({"kind": "backend_error", "text": raw[:240]})
                    continue
                sid = ev.get("session_id")
                if sid and not seen_sid:
                    out.append({"kind": "session_id", "value": sid})
                    seen_sid = True
                kind = ev.get("type", "")
                if kind == "assistant":
                    content = ((ev.get("message") or {}).get("content")) or []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text = (block.get("text") or "").rstrip()
                            if text:
                                out.append({"kind": "text", "text": text})
                        elif btype == "tool_use":
                            input_obj = block.get("input") or {}
                            description = (
                                input_obj.get("description")
                                or _summarize_tool_input(block.get("name") or "", input_obj)
                                or None
                            )
                            tool_ev = {
                                "kind": "tool",
                                "name": block.get("name") or "?",
                                "description": description,
                                "status": "running",
                                "outputLines": None,
                                "startedAtMs": None,
                            }
                            out.append(tool_ev)
                            tuid = block.get("id")
                            if tuid:
                                tool_by_id[tuid] = tool_ev
                elif kind == "user":
                    content = ((ev.get("message") or {}).get("content")) or []
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            continue
                        tool_ev = tool_by_id.get(block.get("tool_use_id") or "")
                        if tool_ev is None:
                            continue
                        tool_ev["status"] = (
                            "error" if block.get("is_error") else "completed"
                        )
                        tool_ev["outputLines"] = _tool_result_line_count(
                            block.get("content"))
                elif kind == "result":
                    out.append({
                        "kind": "result",
                        "reason": ev.get("subtype") or "?",
                        "tokens": _usage_total(ev.get("usage")),
                        "text": (ev.get("result") or "").rstrip()
                                if isinstance(ev.get("result"), str) else "",
                        "isError": bool(ev.get("is_error")),
                    })
                elif kind == "runner_error":
                    out.append({
                        "kind": "backend_event_error",
                        "errorClass": "RunnerError",
                        "errorKind": "backend_error",
                        "message": ev.get("message") or "runner_error",
                    })
    except OSError:
        pass
    return out


def _tool_result_line_count(content: Any) -> int | None:
    """Line count for a tool_result's content, which is either a string
    or a list of {type:"text", text} blocks."""
    if isinstance(content, str):
        return _output_line_count(content)
    if isinstance(content, list):
        total = 0
        seen = False
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                n = _output_line_count(block["text"])
                if n is not None:
                    total += n
                    seen = True
        return total if seen else None
    return None


def _terminal_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The turn's successful `result` event, or None. Errored results
    (subtype error_*, is_error) are handled by _interrupted."""
    for ev in reversed(events):
        if ev["kind"] == "result":
            return ev if not ev.get("isError") else None
    return None


def final_reply(rdir: Path) -> dict[str, Any] | None:
    """If the turn has completed (successful result message), return the
    agent's final answer in the shared finalReply shape. Otherwise None."""
    events = _parse_events(rdir)
    result = _terminal_result(events)
    if result is None:
        return None
    text = result.get("text") or "\n\n".join(
        ev["text"] for ev in events if ev["kind"] == "text"
    ).strip()
    tool_evs = [ev for ev in events if ev["kind"] == "tool"]
    backend_errs = [ev["text"] for ev in events if ev["kind"] == "backend_error"]
    return {
        "text": text,
        "totalToolCalls": len(tool_evs),
        "totalTokens": result.get("tokens"),
        "recentToolCalls": [_tool_one_liner(ev) for ev in tool_evs[-8:]],
        "backendErrors": backend_errs or None,
    }


def live_progress(rdir: Path, *, started_at: int | None = None) -> dict[str, Any]:
    """Small "what's happening now" view for an in-flight turn. Same
    shape as opencode.live_progress."""
    import time
    events = _parse_events(rdir)
    tool_evs = [ev for ev in events if ev["kind"] == "tool"]
    tokens_so_far: int | None = None
    last_reason: str | None = None
    for ev in events:
        if ev["kind"] == "result":
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
    """Detect a recoverable interruption: the turn ended without a
    successful result message AND there's an error signal (errored
    result, synthetic runner_error, or hard stderr error)."""
    events = _parse_events(rdir)
    if _terminal_result(events) is not None:
        return None
    # An errored result message is the clearest terminal-failure signal.
    for ev in reversed(events):
        if ev["kind"] == "result" and ev.get("isError"):
            return {
                "reason": ev.get("text") or ev.get("reason") or "unknown",
                "code": "ResultError",
                "kind": ev.get("reason"),
            }
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
    """Registry-contract entry point; same precedence as the opencode
    backend: finalReply > interrupted (only once terminal) > progress."""
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
        return {"finalReply": None, "progress": None, "interrupted": interrupted, **common}
    return {"finalReply": None, "progress": pg, "interrupted": None, **common}
