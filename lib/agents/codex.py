"""Codex CLI agent runtime for runner.

One process is spawned per conversational turn using `codex exec --json`.
Fresh turns create a persisted Codex thread; later turns use
`codex exec resume <thread-id>`. codex_render.py splits the native JSONL
stream into:

    stdout.log  conversational agent-message text
    stderr.log  complete Codex JSONL plus native stderr diagnostics
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .opencode import (
    _current_turn_byte_bounds,
    _output_line_count,
    _sh_quote,
    _tool_one_liner,
)

_RENDER_SCRIPT = Path(__file__).resolve().parent / "codex_render.py"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

MODELS_NOTE = (
    "Authoritative catalog from `codex debug models`; explicit --model "
    "values must match a model slug in the effective Codex catalog."
)


def render_script_path() -> Path:
    return _RENDER_SCRIPT


def looks_like_session_id(value: str) -> bool:
    return bool(_UUID_RE.fullmatch(value or ""))


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _session_metadata(path: Path) -> dict[str, Any] | None:
    """Read the first session_meta record without loading the transcript."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, raw in enumerate(stream):
                if line_number >= 100:
                    break
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    return payload if isinstance(payload, dict) else None
    except OSError:
        pass
    return None


def find_session(session_id: str, cwd: str | None = None) -> dict[str, Any] | None:
    """Locate a persisted active or archived Codex session.

    Codex stores active transcripts below
    $CODEX_HOME/sessions/YYYY/MM/DD and archived transcripts below
    $CODEX_HOME/archived_sessions. The session_meta payload supplies the
    real project cwd, which runner preserves when adopting a session.
    """
    if not looks_like_session_id(session_id):
        return None
    normalized_id = session_id.lower()
    home = _codex_home()
    candidates: list[Path] = []
    for root in (home / "sessions", home / "archived_sessions"):
        try:
            candidates.extend(root.glob(f"**/rollout-*-{normalized_id}.jsonl"))
        except OSError:
            continue
    if not candidates:
        return None

    caller = str(Path(cwd).resolve()) if cwd else None
    verified: list[tuple[Path, dict[str, Any]]] = []
    for transcript in sorted(set(candidates)):
        meta = _session_metadata(transcript)
        if not meta:
            continue
        stored_id = meta.get("id") or meta.get("session_id")
        if not isinstance(stored_id, str) or stored_id.lower() != normalized_id:
            continue
        verified.append((transcript, meta))
    if not verified:
        return None

    selected_path, selected_meta = verified[0]
    for transcript, meta in verified:
        project_dir = meta.get("cwd")
        if (
            caller
            and isinstance(project_dir, str)
            and project_dir
            and str(Path(project_dir).resolve()) == caller
        ):
            selected_path, selected_meta = transcript, meta
            break

    project_dir = selected_meta.get("cwd")
    if not isinstance(project_dir, str) or not project_dir:
        project_dir = None
    same_project = bool(
        caller
        and project_dir
        and str(Path(project_dir).resolve()) == caller
    )
    return {
        "projectDir": project_dir,
        "sameProject": same_project,
        "transcript": str(selected_path),
    }


def _catalog() -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["codex", "debug", "models"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    return [model for model in (models or []) if isinstance(model, dict)]


def available_models() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for model in _catalog():
        slug = model.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        # The catalog also carries role-specific hidden models (for
        # example the automatic approval reviewer) that are not valid
        # choices for a primary coding-agent turn.
        if model.get("visibility") == "hide":
            continue
        description = " -- ".join(
            part
            for part in (model.get("display_name"), model.get("description"))
            if isinstance(part, str) and part
        )
        entry: dict[str, Any] = {"model": slug}
        if description:
            entry["description"] = description
        out.append(entry)
    return out


def validate_model(model: str) -> tuple[bool, list[str]]:
    valid = [entry["model"] for entry in available_models()]
    return (model in valid, valid)


def build_cmd(
    prompt_path: Path,
    session_id: str | None,
    model: str | None = None,
) -> str:
    """Build an unattended Codex invocation for one turn.

    Global flags intentionally precede `resume`; older Codex versions
    required that ordering for JSON and permission options to propagate.
    The combined bypass flag is supported by both fresh and resumed exec
    turns and matches the Claude backend's unattended permission mode.
    """
    codex_bin = os.environ.get("RUNNER_MCP_CODEX_BIN") or "codex"
    parts = [
        _sh_quote(codex_bin),
        "exec",
        "--json",
        "--color",
        "never",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if model:
        parts.extend(["--model", _sh_quote(model)])
    if session_id:
        parts.extend(["resume", _sh_quote(session_id), "-"])
    else:
        parts.append("-")
    return (
        " ".join(parts)
        + f" < {_sh_quote(str(prompt_path))}"
        + f" | python3 {_sh_quote(str(_RENDER_SCRIPT))}"
    )


def _usage_total(usage: Any) -> int | None:
    """Codex cached/reasoning counters are detail fields, not additive."""
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, (int, float)) and not isinstance(
        output_tokens, (int, float)
    ):
        return None
    return int(input_tokens or 0) + int(output_tokens or 0)


def _is_tool_item(item_type: str) -> bool:
    return item_type in {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "collab_tool_call",
        "computer_use",
        "image_generation",
    }


def _tool_name(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "?")
    if item_type == "command_execution":
        return "shell"
    if item_type == "file_change":
        return "apply_patch"
    if item_type == "mcp_tool_call":
        server = item.get("server")
        tool = item.get("tool")
        if server and tool:
            return f"{server}.{tool}"
        return str(tool or "mcp")
    return item_type


def _tool_description(item: dict[str, Any]) -> str | None:
    item_type = item.get("type")
    if item_type == "command_execution":
        command = item.get("command")
        return str(command)[:240] if command else None
    if item_type == "file_change":
        changes = item.get("changes")
        if isinstance(changes, list):
            paths = [
                str(change.get("path"))
                for change in changes
                if isinstance(change, dict) and change.get("path")
            ]
            return ", ".join(paths[:5]) or None
    if item_type == "mcp_tool_call":
        arguments = item.get("arguments")
        if arguments is not None:
            try:
                return json.dumps(arguments, ensure_ascii=False)[:240]
            except (TypeError, ValueError):
                return str(arguments)[:240]
    for key in ("query", "description", "prompt"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value[:240]
    return None


def _tool_output_lines(item: dict[str, Any]) -> int | None:
    for key in ("aggregated_output", "result", "output"):
        value = item.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            try:
                value = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                value = str(value)
        return _output_line_count(value)
    return None


def _tool_status(item: dict[str, Any], completed: bool) -> str:
    status = item.get("status")
    if isinstance(status, str) and status:
        return status
    if item.get("error") or item.get("exit_code") not in (None, 0):
        return "error"
    return "completed" if completed else "running"


def extract(rdir: Path) -> dict[str, Any]:
    """Extract a conversation-wide summary from raw Codex JSONL."""
    out: dict[str, Any] = {
        "sessionId": None,
        "lastTokens": None,
        "lastReason": None,
        "toolCallCount": 0,
    }
    path = rdir / "stderr.log"
    if not path.exists():
        return out
    tool_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for raw in stream:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "thread.started" and not out["sessionId"]:
                    out["sessionId"] = event.get("thread_id")
                if kind in ("item.started", "item.completed"):
                    item = event.get("item") or {}
                    if _is_tool_item(str(item.get("type") or "")):
                        item_id = str(item.get("id") or "")
                        if item_id:
                            tool_ids.add(item_id)
                elif kind == "turn.completed":
                    out["lastTokens"] = _usage_total(event.get("usage"))
                    out["lastReason"] = "completed"
                elif kind == "turn.failed":
                    out["lastReason"] = "failed"
    except OSError:
        pass
    out["toolCallCount"] = len(tool_ids)
    return out


def _backend_error(event: dict[str, Any]) -> dict[str, Any]:
    error = event.get("error")
    message: str
    code: str
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("details") or error)
        code = str(error.get("code") or error.get("type") or "CodexError")
    elif error:
        message = str(error)
        code = "CodexError"
    else:
        message = str(event.get("message") or event.get("type") or "Codex error")
        code = "CodexError"
    return {
        "kind": "backend_event_error",
        "errorClass": code,
        "errorKind": event.get("type"),
        "message": message[:500],
    }


def _parse_events(rdir: Path) -> list[dict[str, Any]]:
    """Normalize the current turn's Codex JSONL events."""
    out: list[dict[str, Any]] = []
    path = rdir / "stderr.log"
    if not path.exists():
        return out
    byte_start, _ = _current_turn_byte_bounds(rdir, "stderr")
    tools_by_id: dict[str, dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            if byte_start:
                stream.seek(byte_start)
            for raw in stream:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    lowered = raw.lower()
                    hard_error = any(
                        marker in lowered
                        for marker in (
                            "command not found",
                            "no such file or directory",
                            "not logged in",
                            "authentication",
                            "unauthorized",
                            "error",
                            "failed",
                            "panic",
                            "rate limit",
                        )
                    )
                    if hard_error:
                        out.append({"kind": "backend_error", "text": raw[:240]})
                        out.append({
                            "kind": "backend_event_error",
                            "errorClass": "BackendStderr",
                            "errorKind": "backend_error",
                            "message": raw[:500],
                        })
                    continue
                kind = event.get("type")
                if kind == "thread.started":
                    session_id = event.get("thread_id")
                    if session_id:
                        out.append({"kind": "session_id", "value": session_id})
                elif kind in ("item.started", "item.completed"):
                    completed = kind == "item.completed"
                    item = event.get("item") or {}
                    item_type = str(item.get("type") or "")
                    if item_type == "agent_message" and completed:
                        text = item.get("text")
                        if isinstance(text, str) and text.rstrip():
                            out.append({"kind": "text", "text": text.rstrip()})
                        continue
                    if not _is_tool_item(item_type):
                        continue
                    item_id = str(item.get("id") or f"{item_type}:{len(out)}")
                    tool_event = tools_by_id.get(item_id)
                    if tool_event is None:
                        tool_event = {
                            "kind": "tool",
                            "name": _tool_name(item),
                            "description": _tool_description(item),
                            "status": _tool_status(item, completed),
                            "outputLines": _tool_output_lines(item),
                            "startedAtMs": None,
                        }
                        tools_by_id[item_id] = tool_event
                        out.append(tool_event)
                    else:
                        tool_event["name"] = _tool_name(item)
                        tool_event["description"] = (
                            _tool_description(item)
                            or tool_event.get("description")
                        )
                        tool_event["status"] = _tool_status(item, completed)
                        output_lines = _tool_output_lines(item)
                        if output_lines is not None:
                            tool_event["outputLines"] = output_lines
                elif kind == "turn.completed":
                    out.append({
                        "kind": "result",
                        "reason": "completed",
                        "tokens": _usage_total(event.get("usage")),
                    })
                elif kind in ("turn.failed", "error"):
                    out.append(_backend_error(event))
    except OSError:
        pass
    return out


def _terminal_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event["kind"] == "result":
            return event
    return None


def final_reply(rdir: Path) -> dict[str, Any] | None:
    events = _parse_events(rdir)
    result = _terminal_result(events)
    if result is None:
        return None
    text = ""
    for event in events:
        if event["kind"] == "text":
            text = event["text"]
    tools = [event for event in events if event["kind"] == "tool"]
    backend_errors = [
        event["text"] for event in events if event["kind"] == "backend_error"
    ]
    return {
        "text": text,
        "totalToolCalls": len(tools),
        "totalTokens": result.get("tokens"),
        "recentToolCalls": [_tool_one_liner(event) for event in tools[-8:]],
        "backendErrors": backend_errors or None,
    }


def live_progress(
    rdir: Path,
    *,
    started_at: int | None = None,
) -> dict[str, Any]:
    events = _parse_events(rdir)
    tools = [event for event in events if event["kind"] == "tool"]
    result = _terminal_result(events)
    current = None
    if tools:
        latest = tools[-1]
        current = {
            "tool": latest["name"],
            "description": latest.get("description"),
            "status": latest["status"],
            "startedAtMs": latest.get("startedAtMs"),
        }
    backend_errors = [
        event["text"] for event in events if event["kind"] == "backend_error"
    ]
    return {
        "currentActivity": current,
        "tokensSoFar": result.get("tokens") if result else None,
        "lastReason": result.get("reason") if result else None,
        "toolCallCount": len(tools),
        "recentToolCalls": [_tool_one_liner(event) for event in tools[-5:]],
        "turnDurationSec": int(time.time()) - started_at if started_at else None,
        "backendErrors": backend_errors or None,
    }


def _interrupted(rdir: Path) -> dict[str, Any] | None:
    events = _parse_events(rdir)
    if _terminal_result(events) is not None:
        return None
    error = None
    for event in events:
        if event["kind"] == "backend_event_error":
            error = event
    if error is None:
        return None
    return {
        "reason": error.get("message") or "Codex turn failed",
        "code": error.get("errorClass") or "CodexError",
        "kind": error.get("errorKind"),
    }


def compact_view(
    rdir: Path,
    *,
    terminal: bool,
    started_at: int | None = None,
) -> dict[str, Any]:
    progress = live_progress(rdir, started_at=started_at)
    common = {
        "tokensSoFar": progress["tokensSoFar"],
        "lastReason": progress["lastReason"],
        "toolCallCount": progress["toolCallCount"],
        "turnDurationSec": progress["turnDurationSec"],
    }
    reply = final_reply(rdir)
    if reply is not None:
        return {
            "finalReply": reply,
            "progress": None,
            "interrupted": None,
            **common,
        }
    interrupted = _interrupted(rdir)
    if interrupted is not None and terminal:
        return {
            "finalReply": None,
            "progress": None,
            "interrupted": interrupted,
            **common,
        }
    return {
        "finalReply": None,
        "progress": progress,
        "interrupted": None,
        **common,
    }
