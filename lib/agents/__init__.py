"""
runner agent runtimes.

Each module in this package represents a backend the
`research_and_code_assistant_agent` MCP tool can dispatch to. A runtime
module exposes three functions:

    build_cmd(prompt_path: Path, session_id: str | None) -> str
        The shell command to spawn for one conversational turn. Designed
        so the command's stdout becomes runner's stdout.log (rendered
        transcript) and the command's stderr becomes runner's stderr.log
        (raw event stream for drill-down).

    extract(rdir: Path) -> dict
        Scan a run dir's logs and return a summary dict including the
        backend-specific session id under key 'sessionId'. Called lazily
        by runner on every read; first successful extraction is persisted
        to meta.json so it survives across calls.

    compact_view(rdir: Path, *, terminal: bool, started_at: int|None) -> dict
        Synthesize the agent-facing response from the backend's event
        stream. Returns:
          {
            finalReply: dict|None,     # focused final answer, when terminal
                # { text, totalToolCalls, totalTokens, recentToolCalls,
                #   backendErrors }
            progress:   dict|None,     # in-flight progress, when running
                # { currentActivity, tokensSoFar, lastReason, toolCallCount,
                #   recentToolCalls, turnDurationSec, backendErrors }
            # Common summary fields lifted regardless of state:
            tokensSoFar: int|None, lastReason: str|None,
            toolCallCount: int, turnDurationSec: int|None,
          }
        Each backend implements this in its own shape since event formats
        differ. The key design rule: `finalReply.text` carries the sub-
        agent's actual answer ONLY (its conclusions, not the tool work).
        Tool-heavy turns collapse to a count + recent tail in
        `recentToolCalls`, so the response stays small even for marathon
        turns. The full rendered transcript stays on disk at stdout.log
        for runner_grep / runner_section drill-down.

Adding a new agent backend (e.g. 'claude') = one new module in this
package implementing those three functions, plus one new entry in
AGENT_REGISTRY below.
"""

from __future__ import annotations

from . import opencode

AGENT_REGISTRY = {
    "opencode": opencode,
    # "claude": claude,    # future
}


def get(name: str):
    """Look up an agent runtime module by name. Returns None if unknown."""
    return AGENT_REGISTRY.get(name)


def names() -> list[str]:
    return sorted(AGENT_REGISTRY.keys())


# --- Turn-cursor helpers --------------------------------------------------
# Sub-agent runs use append-only stdout.log/stderr.log across the entire
# conversation. meta.agentTurnCursors is a list of per-turn anchors:
#   [{turn, startedAt, stdoutByte, stderrByte, stdoutLine, stderrLine}, ...]
# Position i is the byte/line offset of the START of turn (i+1) -- i.e.
# the EOF of the log at the moment turn (i+1) was spawned. The helpers
# below convert a turn number into a (start, end) slice on the
# corresponding log so readers (compact_view, runner_grep, runner_section)
# can scope to one turn cleanly.

def turn_bounds(meta: dict, which: str, turn: int | None = None) -> tuple[int, int]:
    """Return (byte_start, byte_end) for the requested turn's slice.

    `which` is 'stdout' or 'stderr'. `turn` is 1-indexed; None means
    'current turn' (last cursor entry). byte_end is the start of the
    NEXT turn (exclusive). For the latest turn, byte_end is sys.maxsize
    -- callers just read to EOF naturally.

    Safe fallback: returns (0, sys.maxsize) when meta has no
    agentTurnCursors (legacy / non-agent runs) or the turn index is
    out of range. This means readers get the WHOLE file if cursors are
    missing -- never silently scope to an empty range.
    """
    import sys
    cursors = meta.get("agentTurnCursors") or []
    if not cursors or which not in ("stdout", "stderr"):
        return (0, sys.maxsize)
    byte_key = "stdoutByte" if which == "stdout" else "stderrByte"
    idx = len(cursors) - 1 if turn is None else (turn - 1)
    if idx < 0 or idx >= len(cursors):
        return (0, sys.maxsize)
    start = int(cursors[idx].get(byte_key, 0))
    if idx + 1 < len(cursors):
        end = int(cursors[idx + 1].get(byte_key, sys.maxsize))
    else:
        end = sys.maxsize
    return (start, end)


def turn_line_bounds(meta: dict, which: str, turn: int | None = None) -> tuple[int, int]:
    """Same as turn_bounds() but in 0-indexed line numbers.

    Used by line-oriented readers (runner_grep / runner_section).
    line_end is exclusive. For the latest turn, line_end is sys.maxsize.
    """
    import sys
    cursors = meta.get("agentTurnCursors") or []
    if not cursors or which not in ("stdout", "stderr"):
        return (0, sys.maxsize)
    line_key = "stdoutLine" if which == "stdout" else "stderrLine"
    idx = len(cursors) - 1 if turn is None else (turn - 1)
    if idx < 0 or idx >= len(cursors):
        return (0, sys.maxsize)
    start = int(cursors[idx].get(line_key, 0))
    if idx + 1 < len(cursors):
        end = int(cursors[idx + 1].get(line_key, sys.maxsize))
    else:
        end = sys.maxsize
    return (start, end)
