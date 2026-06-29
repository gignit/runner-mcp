#!/usr/bin/env node
/**
 * runner MCP server
 *
 * Exposes the runner toolset. Three workflows the runner is built for:
 *
 *   1. ONE-SHOT BUILDS / TESTS:
 *      runner_start { cmd: "go test ./...", blocking: true } -> returns
 *      structured per-package status when complete. Auto-detects go test
 *      output (no instrumentation needed). Gates filter pipes
 *      (head/tail/grep) and multi-step &&/||/; chains -- runs your cmd
 *      exactly or returns a how-to message; never rewrites it.
 *
 *   2. LONG-RUNNING SERVICES (dev servers, watchers, file pollers):
 *      runner_start { cmd: "npm run dev", blocking: false } once.
 *      runner_wait_for { runId, pattern: "ready" } to block until ready.
 *      runner_list { state: "running" } to see all live services with
 *      detected endpoints (URLs/ports), restart counts, last log line.
 *      runner_restart { runId } to refresh the same process under the
 *      same runId. runner_kill to stop. NEVER spawn the same service
 *      twice -- restart, don't re-start.
 *
 *   3. INSTRUMENTED SCRIPTS:
 *      Source the runnerlog.sh helper (call runner_helpers for its exact
 *      path) in your script and emit sections/metrics/events.
 *      runner_status returns structured progress.
 *
 * Tools:
 *   runner_guide     - Optional deeper reference (the tool descriptions
 *                      themselves are self-sufficient).
 *   runner_helpers   - paths + snippets for instrumenting scripts.
 *   runner_start     - spawn a command. blocking:true (default) waits up
 *                      to ~45s; if still running, agent follows up via
 *                      runner_status. The job is NEVER killed by the wait.
 *   runner_restart   - kill + respawn under same runId (services).
 *   runner_status    - delta-aware status check; surfaces endpoints + restartCount.
 *   runner_section   - drill into one section's detail.
 *   runner_wait_for  - block until a regex matches in logs (service ready signal).
 *   runner_grep      - regex search of stdout.log + stderr.log.
 *   runner_list      - scoreboard: state + lastLine + endpoints for every run.
 *   runner_kill      - SIGKILL the run's process group.
 *   runner_purge     - remove run directories.
 *   research_and_code_assistant_agent
 *                    - delegate a self-contained task to a peer coding
 *                      agent (opencode today; claude/others pluggable).
 *                      runId-keyed multi-turn conversations work just
 *                      like every other runner tool; backend session id
 *                      is captured internally.
 *
 * All tools shell out to runner_core.py. The MCP server is just argv
 * translation + JSON pass-through.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
  Tool,
} from "@modelcontextprotocol/sdk/types.js";
import { spawn } from "child_process";
import { existsSync, readFileSync, appendFileSync, mkdirSync } from "fs";
import { join, resolve } from "path";
import { homedir } from "os";

// ---------------------------------------------------------------------------
// Install location (XDG data dir) + core CLI resolution
// ---------------------------------------------------------------------------

// The XDG data directory where the installer lays down the payload:
//   $XDG_DATA_HOME/runner-mcp/  (default ~/.local/share/runner-mcp/ on
//   Linux and macOS). We honor $XDG_DATA_HOME when set so the path is
//   relocatable, matching modern CLI conventions.
function xdgDataHome(): string {
  const x = process.env.XDG_DATA_HOME;
  if (x && x.trim()) return x;
  return join(homedir(), ".local", "share");
}

const DATA_DIR = join(xdgDataHome(), "runner-mcp");

// Locate runner_core.py. Resolution order:
//   1. install layout: <dataDir>/runner-mcp/core/runner_core.py, with the
//      MCP at <dataDir>/runner-mcp/mcp/dist/index.js
//   2. dev layout: relative to this file in the source tree
//   3. an explicit override via $RUNNER_MCP_HOME (points at an install root)
function findCorePath(): string {
  const here = new URL(import.meta.url).pathname; // .../mcp/dist/index.js
  const candidates: string[] = [];
  const override = process.env.RUNNER_MCP_HOME;
  if (override && override.trim()) {
    candidates.push(join(override, "core", "runner_core.py"));
  }
  candidates.push(
    join(DATA_DIR, "core", "runner_core.py"),          // installed payload
    resolve(here, "../../../core/runner_core.py"),     // dev: <repo>/core from mcp/dist/
    resolve(here, "../../core/runner_core.py"),         // dev: <repo>/core from mcp/src/
  );
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  throw new Error(`runner_core.py not found; tried: ${candidates.join(", ")}`);
}

const CORE_PATH = findCorePath();

// ---------------------------------------------------------------------------
// CLI invocation
// ---------------------------------------------------------------------------

interface ExecResult {
  stdout: string;
  stderr: string;
  code: number;
}

// Diagnostic logging. Enabled when RUNNER_MCP_DEBUG is set; writes
// timestamped JSONL to the XDG state dir
// ($XDG_STATE_HOME/runner-mcp/mcp-debug.log, default
// ~/.local/state/runner-mcp/mcp-debug.log) when set to "1"/"true", or to
// the explicit path given when RUNNER_MCP_DEBUG is a path. Never writes to
// stdout (that would corrupt the MCP stdio transport).
function xdgStateHome(): string {
  const x = process.env.XDG_STATE_HOME;
  if (x && x.trim()) return x;
  return join(homedir(), ".local", "state");
}

const DEBUG_PATH = (() => {
  const v = process.env.RUNNER_MCP_DEBUG;
  if (!v) return null;
  if (v === "1" || v === "true") {
    const stateDir = join(xdgStateHome(), "runner-mcp");
    try {
      mkdirSync(stateDir, { recursive: true });
    } catch {
      // fall through; appendFileSync will surface nothing (logging is best-effort)
    }
    return join(stateDir, "mcp-debug.log");
  }
  return v;
})();

function dlog(event: string, fields: Record<string, unknown>) {
  if (!DEBUG_PATH) return;
  try {
    const line = JSON.stringify({ ts: new Date().toISOString(), t: Date.now(), event, ...fields }) + "\n";
    appendFileSync(DEBUG_PATH, line);
  } catch {
    // never let logging break the server
  }
}

function execCore(args: string[]): Promise<ExecResult> {
  return new Promise((resolveExec, rejectExec) => {
    const t0 = Date.now();
    dlog("execCore.spawn", { args });
    const proc = spawn("python3", [CORE_PATH, ...args], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    proc.stdout.on("data", (d) => (stdout += d.toString()));
    proc.stderr.on("data", (d) => (stderr += d.toString()));

    // Resolve on the IMMEDIATE child's `exit`, NOT on `close`.
    //
    // Why: `close` only fires once every stdio pipe reaches EOF, which
    // requires EVERY process holding the write end to close it. The runner
    // core double-forks and detaches a daemon (and, for runner_start /
    // sub-agent turns, the user's command may background long-lived
    // children -- terraform, ssh, a nested `opencode run`). Those
    // descendants INHERIT this child's stdout/stderr pipe fds, so the pipe
    // stays open for as long as the detached tree lives -- minutes or
    // forever. With `close`, a `blocking:false` dispatch that the core
    // already answered in <1s would not return to the MCP caller until the
    // whole background tree exited, which manifests as the tool "hanging"
    // despite blocking:false (the caller is forced to Escape).
    //
    // `exit` fires the moment the python core process itself terminates,
    // regardless of inherited fds. We drain whatever it buffered, then
    // destroy our pipe ends so the dangling read fds don't keep the event
    // loop (or the held write end) alive.
    const finish = (code: number | null, via: string) => {
      if (settled) return;
      settled = true;
      try {
        proc.stdout?.destroy();
        proc.stderr?.destroy();
      } catch {
        // best-effort
      }
      dlog("execCore.finish", {
        args,
        via,
        elapsedMs: Date.now() - t0,
        code,
        stdoutLen: stdout.length,
        stderrLen: stderr.length,
      });
      resolveExec({ stdout, stderr, code: code ?? -1 });
    };

    proc.on("error", (err) => {
      dlog("execCore.error", { args, elapsedMs: Date.now() - t0, err: String(err) });
      if (!settled) {
        settled = true;
        rejectExec(err);
      }
    });
    // `exit` => the python process has terminated. Defer one tick so any
    // final stdout/stderr 'data' events already queued are flushed into our
    // buffers before we settle.
    proc.on("exit", (code) => {
      setImmediate(() => finish(code, "exit"));
    });
    // Keep `close` as a backstop: if it ever fires first (no inherited fds
    // held the pipe open), settle from here too. Whichever fires first wins.
    proc.on("close", (code) => finish(code, "close"));
  });
}

function toolText(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    isError,
  };
}

// Tools that benefit from a "surface this to the user" nudge in their
// response. The reminder is appended as a separate trailing content block
// so it reads as a directive to the agent rather than another data field
// to serialize back. Tools NOT in this set (runner_grep, runner_section,
// runner_kill, runner_purge, runner_helpers, runner_guide, runner_restart)
// are agent-internal mechanics whose output rarely contains anything the
// user directly needs to see.
const TOOLS_WITH_USER_REMINDER = new Set<string>([
  "runner_start",
  "runner_status",
  "runner_wait_for",
  "runner_list",
  "research_and_code_assistant_agent",
]);

const USER_REMINDER_TEXT =
  "---\n" +
  "> runner reminder: Surface any relevant info from this response to the user " +
  "(URLs, ports, results, failed tests, warnings, restartCount, etc.) so they " +
  "know what happened and how to act on it. Don't just report \"done\".";

function withUserReminder(toolName: string, payload: ReturnType<typeof toolText>) {
  if (!TOOLS_WITH_USER_REMINDER.has(toolName) || payload.isError) {
    return payload;
  }
  return {
    ...payload,
    content: [
      ...payload.content,
      { type: "text" as const, text: USER_REMINDER_TEXT },
    ],
  };
}

async function dispatch(toolName: string, args: Record<string, any>) {
  // Build argv based on tool name. The runner has no required reading;
  // each tool's description is self-sufficient. runner_guide is available
  // for deeper context but is not gated on.
  dlog("dispatch.enter", {
    tool: toolName,
    argKeys: args ? Object.keys(args) : [],
    blockingRaw: args?.blocking,
    blockingType: typeof args?.blocking,
    blockingStrictFalse: args?.blocking === false,
    hasAsk: args ? "ask" in args : false,
    hasRunId: args ? "runId" in args : false,
    argsJson: (() => { try { return JSON.stringify(args); } catch { return "<unserializable>"; } })(),
  });
  const cliArgs: string[] = [];

  switch (toolName) {
    case "runner_guide": {
      cliArgs.push("guide");
      break;
    }
    case "runner_helpers": {
      cliArgs.push("helpers");
      if (args?.pretty !== false) cliArgs.push("--pretty");
      break;
    }
    case "runner_start": {
      cliArgs.push("start");
      if (!args?.cmd) return toolText(JSON.stringify({ error: "cmd is required" }), true);
      cliArgs.push("--cmd", String(args.cmd));
      if (args.cwd) cliArgs.push("--cwd", String(args.cwd));
      if (args.name) cliArgs.push("--name", String(args.name));
      if (args.description) cliArgs.push("--description", String(args.description));
      // blocking defaults to true on the Python side; only need to pass
      // --no-blocking when the caller explicitly wants fire-and-forget.
      if (args.blocking === false) {
        cliArgs.push("--no-blocking");
      } else {
        cliArgs.push("--blocking");
      }
      if (args.parser) cliArgs.push("--parser", String(args.parser));
      if (args.noScrub) cliArgs.push("--no-scrub");
      cliArgs.push("--pretty");
      break;
    }
    case "runner_restart": {
      cliArgs.push("restart");
      if (!args?.runId) return toolText(JSON.stringify({ error: "runId is required" }), true);
      cliArgs.push("--run-id", String(args.runId));
      if (args.cwd) cliArgs.push("--cwd", String(args.cwd));
      break;
    }
    case "runner_status": {
      cliArgs.push("status");
      if (!args?.runId) return toolText(JSON.stringify({ error: "runId is required" }), true);
      cliArgs.push("--run-id", String(args.runId));
      if (args.agent) cliArgs.push("--agent", String(args.agent));
      if (args.since !== undefined) cliArgs.push("--since", String(args.since));
      if (args.cwd) cliArgs.push("--cwd", String(args.cwd));
      if (args.verbose) cliArgs.push("--verbose");
      if (args.wait === true) cliArgs.push("--wait");
      else if (args.wait === false) cliArgs.push("--no-wait");
      if (args.grep) cliArgs.push("--grep", String(args.grep));
      if (args.grepStream) cliArgs.push("--grep-stream", String(args.grepStream));
      if (args.grepA !== undefined) cliArgs.push("--grep-a", String(args.grepA));
      if (args.grepB !== undefined) cliArgs.push("--grep-b", String(args.grepB));
      if (args.grepLimit !== undefined) cliArgs.push("--grep-limit", String(args.grepLimit));
      if (args.grepIgnoreCase) cliArgs.push("--grep-ignore-case");
      cliArgs.push("--pretty");
      break;
    }
    case "runner_section": {
      cliArgs.push("section");
      if (!args?.runId) return toolText(JSON.stringify({ error: "runId is required" }), true);
      if (!args?.name) return toolText(JSON.stringify({ error: "name is required" }), true);
      cliArgs.push("--run-id", String(args.runId));
      cliArgs.push("--name", String(args.name));
      if (args.occurrence) cliArgs.push("--occurrence", String(args.occurrence));
      if (args.cwd) cliArgs.push("--cwd", String(args.cwd));
      if (args.verbose) cliArgs.push("--verbose");
      if (args.grep) cliArgs.push("--grep", String(args.grep));
      if (args.grepStream) cliArgs.push("--grep-stream", String(args.grepStream));
      if (args.grepA !== undefined) cliArgs.push("--grep-a", String(args.grepA));
      if (args.grepB !== undefined) cliArgs.push("--grep-b", String(args.grepB));
      if (args.grepLimit !== undefined) cliArgs.push("--grep-limit", String(args.grepLimit));
      if (args.grepIgnoreCase) cliArgs.push("--grep-ignore-case");
      cliArgs.push("--pretty");
      break;
    }
    case "runner_wait_for": {
      cliArgs.push("wait-for");
      if (!args?.runId) return toolText(JSON.stringify({ error: "runId is required" }), true);
      if (!args?.pattern) return toolText(JSON.stringify({ error: "pattern is required" }), true);
      cliArgs.push("--run-id", String(args.runId));
      cliArgs.push("--pattern", String(args.pattern));
      if (args.stream) cliArgs.push("--stream", String(args.stream));
      if (args.ignoreCase) cliArgs.push("--ignore-case");
      if (args.cwd) cliArgs.push("--cwd", String(args.cwd));
      cliArgs.push("--pretty");
      break;
    }
    case "runner_grep": {
      cliArgs.push("grep");
      if (!args?.runId) return toolText(JSON.stringify({ error: "runId is required" }), true);
      if (!args?.pattern) return toolText(JSON.stringify({ error: "pattern is required" }), true);
      cliArgs.push("--run-id", String(args.runId));
      cliArgs.push("--pattern", String(args.pattern));
      if (args.stream) cliArgs.push("--stream", String(args.stream));
      if (args.A !== undefined) cliArgs.push("--A", String(args.A));
      if (args.B !== undefined) cliArgs.push("--B", String(args.B));
      if (args.limit !== undefined) cliArgs.push("--limit", String(args.limit));
      if (args.ignoreCase) cliArgs.push("--ignore-case");
      if (args.allTurns) cliArgs.push("--all-turns");
      if (args.cwd) cliArgs.push("--cwd", String(args.cwd));
      cliArgs.push("--pretty");
      break;
    }
    case "runner_list": {
      cliArgs.push("list");
      if (args?.cwd) cliArgs.push("--cwd", String(args.cwd));
      if (args?.state) cliArgs.push("--state", String(args.state));
      if (args?.name) cliArgs.push("--name", String(args.name));
      if (args?.limit) cliArgs.push("--limit", String(args.limit));
      cliArgs.push("--pretty");
      break;
    }
    case "runner_kill": {
      cliArgs.push("kill");
      if (!args?.runId) return toolText(JSON.stringify({ error: "runId is required" }), true);
      cliArgs.push("--run-id", String(args.runId));
      if (args.cwd) cliArgs.push("--cwd", String(args.cwd));
      break;
    }
    case "runner_purge": {
      cliArgs.push("purge");
      if (args?.runId) cliArgs.push("--run-id", String(args.runId));
      if (args?.olderThan !== undefined) cliArgs.push("--older-than", String(args.olderThan));
      if (args?.result) cliArgs.push("--result", String(args.result));
      if (args?.cwd) cliArgs.push("--cwd", String(args.cwd));
      cliArgs.push("--pretty");
      break;
    }
    case "research_and_code_assistant_agent": {
      cliArgs.push("agent");
      // Python side validates: either --ask (to send/adopt) or --run-id
      // alone (to poll for the existing turn's response) is required.
      if (args?.ask) cliArgs.push("--ask", String(args.ask));
      if (args?.agent) cliArgs.push("--agent", String(args.agent));
      if (args?.runId) cliArgs.push("--run-id", String(args.runId));
      if (args?.cwd) cliArgs.push("--cwd", String(args.cwd));
      // blocking defaults to true on the Python side; pass --no-blocking
      // only when the caller explicitly wants fire-and-forget.
      if (args?.blocking === false) {
        cliArgs.push("--no-blocking");
      }
      cliArgs.push("--pretty");
      break;
    }
    default:
      return toolText(`Unknown tool: ${toolName}`, true);
  }

  dlog("dispatch.cliArgs", { tool: toolName, cliArgs, noBlockingPassed: cliArgs.includes("--no-blocking") });
  const result = await execCore(cliArgs);
  if (result.code !== 0) {
    // Pass through stderr (which has structured error JSON when our CLI
    // writes one) so the agent sees the cause.
    const text = result.stderr.trim() || result.stdout.trim() || `runner CLI exited ${result.code}`;
    return toolText(text, true);
  }
  return withUserReminder(toolName, toolText(result.stdout));
}

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

const TOOLS: Tool[] = [
  {
    name: "runner_guide",
    description:
      "Optional: returns the full runner reference (workflows, response fields, anti-patterns, recipes). Call when a response field surprises you or you need deeper context.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "runner_helpers",
    description:
      "Paths + snippets for the runnerlog helpers (bash/python/CLI). Only needed if you're writing an instrumented script that emits section/metric/event markers. See runner_guide.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "runner_start",
    description:
      "Run a tracked operation (build, test, install, CLI call, or service). Returns runId + structured output. Runs your cmd EXACTLY -- it is never rewritten. Filter pipes (| head/tail/grep) and multi-step &&/||/; chains are GATED: instead of running, you get a how-to message (use runner_grep/section/status to filter; put multi-step work in a .runner/scripts/ script -- call runner_helpers). noScrub:true runs the exact string verbatim. Restart services with runner_restart, not a second runner_start. See runner_guide.",
    inputSchema: {
      type: "object",
      required: ["cmd"],
      properties: {
        cmd: { type: "string", description: "Shell command (passed to bash -c)." },
        cwd: { type: "string", description: "Spawn working directory. Run storage is always your project root's .runner/, regardless of cwd." },
        name: { type: "string", description: "Short label (default: auto-derived, e.g. `cd /p && go test ./...` -> `go-test`). Used in runner_list filtering." },
        description: { type: "string", description: "Free-text intent (distinct from name). Surfaced in runner_list/runner_status for later recall." },
        blocking: { type: "boolean", description: "Default true. Holds for up to ~45s; on elapse response.stillRunning=true, follow up with runner_status. Set false for services." },
        parser: { type: "string", enum: ["auto", "none", "go-test"], description: "Output adapter (default 'auto')." },
        noScrub: { type: "boolean", description: "Bypass the command gate and run the exact cmd string verbatim, including filter pipes and &&/||/; chains (rarely needed)." },
      },
    },
  },
  {
    name: "runner_restart",
    description:
      "Refresh a service under the same runId (kills + respawns). Use this -- not a second runner_start -- to avoid duplicate processes. See runner_guide.",
    inputSchema: {
      type: "object",
      required: ["runId"],
      properties: {
        runId: { type: "string", description: "The runId returned by runner_start." },
        cwd: { type: "string", description: "Working directory used to resolve the run root." },
      },
    },
  },
  {
    name: "runner_status",
    description:
      "Poll a run. Returns terminal/result/exitCode + structured fields (testSummary, failedSections, warnings, endpoints) and the delta of new events. For blocking runs, auto-waits ~45s; just call again until terminal:true.",
    inputSchema: {
      type: "object",
      required: ["runId"],
      properties: {
        runId: { type: "string" },
        agent: { type: "string", description: "Delta-cursor identifier (default 'default')." },
        since: { type: "integer", description: "Line cursor override." },
        cwd: { type: "string" },
        verbose: { type: "boolean", description: "Include line numbers, full sections, raw events, meta." },
        wait: { type: "boolean", description: "Override auto-wait (default: on for blocking, off for services)." },
        grep: { type: "string", description: "Embed regex matches from stdout+stderr in the response." },
        grepStream: { type: "string", enum: ["stdout", "stderr", "both"] },
        grepA: { type: "integer", description: "Lines of context after each match." },
        grepB: { type: "integer", description: "Lines of context before each match." },
        grepLimit: { type: "integer", description: "Default 200." },
        grepIgnoreCase: { type: "boolean" },
      },
    },
  },
  {
    name: "runner_section",
    description:
      "Drill into one section (e.g. a failed go-test package). Returns its events, metrics, status, plus log/stderr tails. Passing tests filtered by default; pass verbose:true for everything.",
    inputSchema: {
      type: "object",
      required: ["runId", "name"],
      properties: {
        runId: { type: "string" },
        name: { type: "string", description: "Section name; for go-test, the package import path." },
        occurrence: { type: "integer", description: "1-based when the name repeats. Default 1." },
        cwd: { type: "string" },
        verbose: { type: "boolean", description: "Include passing/skipping test events." },
        grep: { type: "string" },
        grepStream: { type: "string", enum: ["stdout", "stderr", "both"] },
        grepA: { type: "integer" },
        grepB: { type: "integer" },
        grepLimit: { type: "integer" },
        grepIgnoreCase: { type: "boolean" },
      },
    },
  },
  {
    name: "runner_wait_for",
    description:
      "Block until a regex matches in the run's logs (or the run exits, or ~45s elapses). Returns outcome: matched|exited|timeout. Use after a non-blocking start to wait for the service ready signal. See runner_guide for common framework patterns.",
    inputSchema: {
      type: "object",
      required: ["runId", "pattern"],
      properties: {
        runId: { type: "string" },
        pattern: { type: "string", description: "Python regex." },
        stream: { type: "string", enum: ["stdout", "stderr", "both"], description: "Default 'both'." },
        ignoreCase: { type: "boolean" },
        cwd: { type: "string" },
      },
    },
  },
  {
    name: "runner_grep",
    description:
      "Regex search the run's stdout+stderr. Returns matches with stream, lineNo, optional context. Reach for runner_status/runner_section first for structured data; use grep for raw text patterns. For SUB-AGENT runs the conversation logs are append-only across all turns, so this tool DEFAULTS to scoping the current turn's slice -- pass allTurns:true to search the full history (each match then includes a `turn: N` field).",
    inputSchema: {
      type: "object",
      required: ["runId", "pattern"],
      properties: {
        runId: { type: "string" },
        pattern: { type: "string", description: "Python regex." },
        stream: { type: "string", enum: ["stdout", "stderr", "both"], description: "Default 'both'." },
        A: { type: "integer", description: "Context lines after." },
        B: { type: "integer", description: "Context lines before." },
        limit: { type: "integer", description: "Default 200." },
        ignoreCase: { type: "boolean" },
        allTurns: { type: "boolean", description: "Sub-agent runs only: search ALL turns of the conversation, not just the current one. Matches are annotated with `turn: N` so you can place each hit. Default false (current turn only)." },
        cwd: { type: "string" },
      },
    },
  },
  {
    name: "runner_list",
    description:
      "Scoreboard of all runs in your project. Each entry has runId, name, state, result, durationSec, pid, restartCount, lastLine + lastLineAgeSec, endpoints, failedSections. Sub-agent conversations (research_and_code_assistant_agent) include an `agent` block with runtime + turn count -- use this to find a conversation you want to continue. Filter by state and/or name. Newest first.",
    inputSchema: {
      type: "object",
      properties: {
        state: { type: "string", enum: ["starting", "running", "exited"] },
        name: { type: "string", description: "Regex or substring." },
        limit: { type: "integer" },
        cwd: { type: "string" },
      },
    },
  },
  {
    name: "runner_kill",
    description:
      "SIGKILL the run's process group (reaps children). Returns {runId, killed, killedAt, pid}.",
    inputSchema: {
      type: "object",
      required: ["runId"],
      properties: {
        runId: { type: "string" },
        cwd: { type: "string" },
      },
    },
  },
  {
    name: "runner_purge",
    description:
      "Remove run dirs. Args (precedence): runId | olderThan(sec) | result(success|failed) | none=all-terminal. Active runs are preserved and reported in kept.active. Returns {purged, kept, freedBytesTotal, summary}.",
    inputSchema: {
      type: "object",
      properties: {
        runId: { type: "string", description: "Purge regardless of state." },
        olderThan: { type: "integer", description: "Terminal runs older than N seconds." },
        result: { type: "string", enum: ["success", "failed"] },
        cwd: { type: "string" },
      },
    },
  },
  {
    name: "research_and_code_assistant_agent",
    description:
      "Delegate work to a peer coding agent that runs in this same project with the same file access. Two modes, one tool: (a) Pass `ask` (with optional `runId`) to SEND a prompt -- new conversation if runId is absent, continue existing if runId is a runner runId, or adopt an external session if runId is a backend session id like `ses_...`. (b) Pass only `runId` with no `ask` to POLL for the response on an existing turn -- returns `finalReply` when ready, or `stillRunning` + progress hint if the turn isn't done. Either way the response carries the sub-agent's complete reply when it finishes (no transcript dump). Find existing conversations via `runner_list` (entries with an `agent` block). `runner_kill` cancels an in-flight turn. Good for: second opinions, parallel investigations, offloading a self-contained subtask.",
    inputSchema: {
      type: "object",
      properties: {
        ask: { type: "string", description: "Prompt to send. Multi-line is fine; passed via stdin so quoting is never an issue. Omit (with runId) to poll for the response on an existing turn instead of sending a new one." },
        runId: { type: "string", description: "Continue an existing conversation (runner runId), adopt an external session (backend session id like `ses_...` -- pass with `ask`), or poll for the response on an existing run (runner runId, omit `ask`)." },
        agent: { type: "string", enum: ["opencode"], description: "Backend agent (default: opencode). More backends can be added by dropping a module in lib/agents/." },
        cwd: { type: "string", description: "Working directory for the sub-agent. Defaults to the calling agent's cwd." },
        blocking: { type: "boolean", description: "Default true. Holds the response until the turn completes (or ~9 min elapses, returning stillRunning + the followUp hint to call again with just runId). Set false to fire-and-forget: spawn the turn in the background, return the runId immediately, and poll later via the same tool with just runId. Useful for parallel dispatches or kicking off long investigations while you do other work." },
      },
    },
  },
];

// ---------------------------------------------------------------------------
// MCP server
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "runner", version: "0.1.0" },
  { capabilities: { tools: {}, resources: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  return dispatch(name, args ?? {});
});

// Resource: the agent-facing GUIDE
server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: [
    {
      uri: "runner://guide",
      name: "Runner usage guide",
      description: "How to write scripts that work with the runner: protocol, helpers, agent loop pattern.",
      mimeType: "text/markdown",
    },
  ],
}));

server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
  if (request.params.uri === "runner://guide") {
    // Locate GUIDE.md alongside the install
    const guideCandidates = [
      join(DATA_DIR, "docs", "GUIDE.md"),                                  // installed payload
      resolve(new URL(import.meta.url).pathname, "../../../docs/GUIDE.md"), // dev from mcp/dist/
      resolve(new URL(import.meta.url).pathname, "../../docs/GUIDE.md"),    // dev from mcp/src/
    ];
    for (const p of guideCandidates) {
      if (existsSync(p)) {
        return {
          contents: [{ uri: request.params.uri, mimeType: "text/markdown", text: readFileSync(p, "utf-8") }],
        };
      }
    }
    return {
      contents: [{ uri: request.params.uri, mimeType: "text/plain", text: "GUIDE.md not found in install" }],
    };
  }
  throw new Error(`Unknown resource: ${request.params.uri}`);
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("runner MCP fatal:", err);
  process.exit(1);
});
