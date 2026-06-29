#!/usr/bin/env node
/**
 * claude_runner -- one conversational turn of Claude Code via the
 * @anthropic-ai/claude-agent-sdk, shaped for runner's log contract.
 *
 * Pipeline position (spawned by lib/agents/claude.py build_cmd):
 *
 *     node claude_runner.mjs --prompt-file prompt.md [--session SID] [--model M]
 *
 * Outputs (mirroring lib/agents/opencode_render.py's split):
 *
 *   stdout (-> runner's stdout.log):
 *       Only the SUB-AGENT'S CONVERSATIONAL TEXT -- the text blocks of
 *       each assistant message. No tool calls, no metadata. Reads like
 *       a conversation top to bottom.
 *
 *   stderr (-> runner's stderr.log):
 *       The COMPLETE SDK message stream as NDJSON, one message per
 *       line. This is the source of truth for lib/agents/claude.py's
 *       extract() / compact_view(): session_id, tool calls, usage,
 *       and the terminal `result` message all come from here. The
 *       claude subprocess's own stderr chatter is forwarded here too
 *       (free-form text, shape-distinct from NDJSON).
 *
 * SDK resolution: the payload ships the SDK in <root>/mcp/node_modules
 * (installed with --omit=optional; the ~100MB platform-specific vendored
 * binaries are deliberately NOT shipped). Because the vendored binary is
 * absent, we always locate the user's own `claude` executable and hand
 * it to the SDK via pathToClaudeCodeExecutable. `claude` on PATH is a
 * runtime requirement of the claude backend, exactly as `opencode` on
 * PATH is for the opencode backend.
 */

import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// arg parsing (tiny, positional-free)
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const out = { promptFile: null, session: null, model: null, listModels: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--prompt-file") out.promptFile = argv[++i];
    else if (a === "--session") out.session = argv[++i];
    else if (a === "--model") out.model = argv[++i];
    else if (a === "--list-models") out.listModels = true;
  }
  return out;
}

// ---------------------------------------------------------------------------
// error surface -- keep it machine-readable so claude.py can classify
// ---------------------------------------------------------------------------

/** Emit a synthetic NDJSON error event that lib/agents/claude.py maps to
 *  backend_event_error (same interrupt path as an API failure). */
function emitFatal(message) {
  process.stderr.write(
    JSON.stringify({ type: "runner_error", message: String(message) }) + "\n",
  );
}

// ---------------------------------------------------------------------------
// locate the SDK and the claude binary
// ---------------------------------------------------------------------------

function isExecutable(p) {
  try {
    const s = statSync(p);
    return s.isFile() || s.isSymbolicLink();
  } catch {
    return false;
  }
}

/** Standard Claude CLI executable search order: env override,
 *  PATH scan, ~/.local/bin fallback. Returns undefined when not found. */
function findClaudeExecutable() {
  const fromEnv = process.env["RUNNER_MCP_CLAUDE_BIN"];
  if (fromEnv && isExecutable(fromEnv)) return fromEnv;
  const pathDirs = (process.env["PATH"] ?? "").split(delimiter).filter(Boolean);
  for (const dir of pathDirs) {
    const candidate = join(dir, "claude");
    if (isExecutable(candidate)) return candidate;
  }
  const fallback = join(homedir(), ".local", "bin", "claude");
  if (isExecutable(fallback)) return fallback;
  return undefined;
}

/** Import the agent SDK. Candidates, in order:
 *    1. $RUNNER_MCP_CLAUDE_SDK (path to the package dir or sdk.mjs)
 *    2. <root>/mcp/node_modules/... relative to this script -- works in
 *       both the git repo and the installed payload, which share the
 *       lib/ + mcp/node_modules sibling layout.
 *    3. bare specifier, in case the script runs somewhere with its own
 *       node_modules. */
async function importSdk() {
  const candidates = [];
  const fromEnv = process.env["RUNNER_MCP_CLAUDE_SDK"];
  if (fromEnv) {
    candidates.push(fromEnv.endsWith(".mjs") ? fromEnv : join(fromEnv, "sdk.mjs"));
  }
  candidates.push(
    resolve(SCRIPT_DIR, "..", "..", "mcp", "node_modules",
      "@anthropic-ai", "claude-agent-sdk", "sdk.mjs"),
  );
  for (const c of candidates) {
    try {
      statSync(c);
      return await import(pathToFileURL(c).href);
    } catch {
      // try the next candidate
    }
  }
  return await import("@anthropic-ai/claude-agent-sdk");
}

// ---------------------------------------------------------------------------
// render one SDK message: NDJSON to stderr, assistant text to stdout
// ---------------------------------------------------------------------------

function handleMessage(msg) {
  process.stderr.write(JSON.stringify(msg) + "\n");
  if (msg.type !== "assistant") return;
  const content = msg.message?.content;
  if (!Array.isArray(content)) return;
  for (const block of content) {
    if (block?.type === "text" && typeof block.text === "string") {
      const text = block.text.trimEnd();
      if (text) process.stdout.write(text + "\n");
    }
  }
}

// ---------------------------------------------------------------------------
// --list-models: print the SDK's supported models as JSON and exit
// ---------------------------------------------------------------------------

/** Query.supportedModels() without sending a turn: the prompt is a
 *  never-yielding stream, so no user message ever reaches the model.
 *  Prints a JSON array of {value, displayName, description} to stdout. */
async function listModels(sdk, claudeBin) {
  const neverYield = {
    async *[Symbol.asyncIterator]() {
      await new Promise(() => {});
    },
  };
  const q = sdk.query({
    prompt: neverYield,
    options: { cwd: process.cwd(), pathToClaudeCodeExecutable: claudeBin },
  });
  try {
    const models = await q.supportedModels();
    process.stdout.write(JSON.stringify(models) + "\n");
    return 0;
  } finally {
    try { q.close(); } catch { /* best-effort */ }
  }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.promptFile && !args.listModels) {
    emitFatal("claude_runner: --prompt-file is required");
    return 2;
  }
  let prompt = null;
  if (args.promptFile) {
    try {
      prompt = readFileSync(args.promptFile, "utf8");
    } catch (err) {
      emitFatal(`claude_runner: cannot read prompt file: ${err.message ?? err}`);
      return 1;
    }
  }

  let sdk;
  try {
    sdk = await importSdk();
  } catch (err) {
    emitFatal(
      "claude_runner: @anthropic-ai/claude-agent-sdk not found " +
      `(looked under mcp/node_modules; re-run the installer): ${err.message ?? err}`,
    );
    return 1;
  }

  const claudeBin = findClaudeExecutable();
  if (!claudeBin) {
    emitFatal(
      "claude_runner: no `claude` executable found on PATH. The claude " +
      "backend requires Claude Code to be installed (https://claude.com/claude-code).",
    );
    return 1;
  }

  if (args.listModels) {
    try {
      return await listModels(sdk, claudeBin);
    } catch (err) {
      emitFatal(`claude_runner: supportedModels failed: ${err?.message ?? err}`);
      return 1;
    }
  }

  const options = {
    cwd: process.cwd(),
    pathToClaudeCodeExecutable: claudeBin,
    // Headless sub-agent: nobody is present to answer permission
    // prompts, so run unattended -- the same trust level `opencode run`
    // gets on the opencode backend.
    permissionMode: "bypassPermissions",
    ...(args.session ? { resume: args.session } : {}),
    ...(args.model ? { model: args.model } : {}),
    // Forward the subprocess's own stderr chatter into stderr.log as
    // free-form text; on failures it's the only diagnostic there is.
    stderr: (chunk) => {
      const text = String(chunk).trimEnd();
      if (text) process.stderr.write(text + "\n");
    },
  };

  try {
    for await (const msg of sdk.query({ prompt, options })) {
      handleMessage(msg);
    }
  } catch (err) {
    emitFatal(`claude_runner: SDK stream failed: ${err?.message ?? err}`);
    return 1;
  }
  return 0;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    emitFatal(`claude_runner: unexpected failure: ${err?.message ?? err}`);
    process.exit(1);
  },
);
