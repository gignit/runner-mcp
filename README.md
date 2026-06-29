# runner-mcp

An MCP server that lets AI agents **run and manage background tasks** -- dev
servers (vite, backend APIs), builds, tests, long-running scripts -- and
**collaborate with other agents** through true multi-turn conversations.

Agents can start a process and get a `runId` back, monitor its stdout/stderr,
restart it, kill it, and list everything running -- all without losing track
of background processes or flooding their own context with raw log output.
And via the sub-agent tools they can hand work to a peer agent and hold a
real back-and-forth with it, not just fire a one-shot prompt.

Works with **opencode, Claude Code, Codex, Grok, and VS Code / Cursor** on
**macOS and Linux**.
Sub-agent collaboration runs on **opencode** today (other backends can be
added later).

## What it gives an agent

- **Background process control.** Start a dev server or any long-running
  command, then `restart`, `kill`, `list`, and check status by `runId` --
  no orphaned processes, no re-running a command just to see what it printed.
- **Monitoring without log-grepping.** Every call returns decision-first
  JSON (`state`, `result`, `exitCode`, `endpoints`, `stdoutTail`,
  `warnings`) plus only the new output since the agent's last check (a
  per-agent delta cursor), so a long job never blows out the context window.
- **Service-aware.** Detected URLs/ports surface in `endpoints` ("what port
  is vite on?"); known test formats (e.g. `go test`) are parsed into
  structured per-package pass/fail.
- **Agent collaboration.** Delegate a task to a peer agent and continue the
  conversation across turns -- a genuine collaborator that keeps context,
  not a single-use prompt.
- **No more transport timeouts.** Long commands don't trip the MCP timeout:
  blocking runs auto-poll under a single repeated call, and the job is never
  killed by the wait.

---

## Install

```sh
curl -fsSL https://github.com/gignit/runner-mcp/releases/latest/download/install.sh | sh
```

That one line is the supported install method. The installer:

1. Detects your OS + architecture (macOS/Linux, amd64/arm64) and downloads
   the matching release tarball from GitHub Releases (checksum-verified).
2. Installs the payload to `$XDG_DATA_HOME/runner-mcp`
   (default `~/.local/share/runner-mcp`).
3. Symlinks the `runner-mcp` and `runnerlog` CLIs into `~/.local/bin`.
4. Registers the MCP server with every supported agent it finds on your
   machine: **opencode**, **Claude Code**, **Codex**, **Grok**, and
   **VS Code / Cursor** (via each one's user `mcp.json`).

**Requirements at runtime:** `node` and `python3` on your `PATH`
(both are typically already present for agent users).

To pin a specific version:

```sh
curl -fsSL https://github.com/gignit/runner-mcp/releases/download/v0.1.0/install.sh | sh
```

After installing, **restart your agent** so it loads the MCP server. Each
tool's description is self-sufficient -- the agent doesn't need to call
`runner_guide` first.

### Uninstall

The installer doubles as the uninstaller -- same one-line download, with
`--uninstall`. It's a **dry run by default** (prints exactly what it would
remove); add `--yes` to actually remove:

```sh
# See what would be removed (removes nothing):
curl -fsSL https://github.com/gignit/runner-mcp/releases/latest/download/install.sh | sh -s -- --uninstall

# Actually uninstall:
curl -fsSL https://github.com/gignit/runner-mcp/releases/latest/download/install.sh | sh -s -- --uninstall --yes
```

It removes exactly what was installed: the payload dir
(`~/.local/share/runner-mcp`), the `runner-mcp` / `runnerlog` symlinks (only
if they still point into that dir), and the `runner` MCP registration from
every agent (opencode, Claude, Codex, Grok, VS Code / Cursor). **Per-project `<git-root>/.runner/`
run data is never touched** -- so uninstalling and reinstalling a new version
is clean and safe.

### Per-project isolation

Run data is stored in `<git-root>/.runner/` for whatever project the agent
is working in, so each project's agents only see that project's runs. The
runner adds `.runner` to the project's `.git/info/exclude` automatically, so
it never shows up in `git status`. (When invoked outside any git repo, runs
fall back to `~/.local/share/runner-mcp`.)

### Building from source

Contributors can build and dogfood the installer locally:

```sh
make build      # compile for this machine (mcp/dist + bin/runner-mcp)
make install    # build, stage a local tarball, run install.sh against it
make release    # cross-compile all targets + publish a GitHub Release
```

---

## How an agent uses it

The agent-facing usage guide is served by the `runner_guide` MCP tool.
Its full content lives in `docs/GUIDE.md`. The four core workflows:

### 1. One-shot builds, tests, installs (default)

```
runner_start { cmd: "make reinstall", cwd: "/path/to/project" }
runner_start { cmd: "go test ./...", cwd: "/path/to/project" }
runner_start { cmd: "npm run build", cwd: "/path/to/project" }
```

`blocking: true` is the default. The call holds open up to ~45s. If
the run finishes within that window you get terminal status. If not,
the response sets `stillRunning: true` and the agent calls
`runner_status { runId }` -- which auto-waits another ~45s for
blocking-mode runs. **The agent's protocol is just "keep calling
runner_status until terminal:true."** No timing decisions, no sleeps.

The terminal response includes:
- `result`, `exitCode`, `durationSec`
- `stdoutTail` -- last 15 lines of stdout (replaces `| tail -10`)
- `warnings` -- count + sample of `ERROR / FAIL / panic / fatal /
  "connection refused"` lines, even when exit code is 0 (catches
  shell scripts and Makefiles that tolerate partial failures)
- `endpoints` -- detected URLs/ports parsed from the output (vite,
  go, express, uvicorn, rails patterns)

For `go test`, an output adapter synthesizes per-package sections and
returns a `testSummary`:

```json
"testSummary": {
  "status": "failed",
  "packagesRun": 5, "packagesFailed": 1,
  "testsPass": 89, "testsFail": 1, "testsSkip": 0,
  "failedTests": [{ "package": "example/pkg/store", "test": "TestEtag" }],
  "nextCalls": [
    { "tool": "runner_section",
      "args": { "runId": "...", "name": "example/pkg/store" },
      "purpose": "Inspect failed package example/pkg/store ..." }
  ]
}
```

Passing tests are filtered from `delta.newEvents` to keep the
response compact (a 200-test run returns ~10 events, not 200).

### 2. Long-running services

```
runner_start { cmd: "npm run dev", name: "fe", blocking: false, cwd: "..." }
-> { runId, pid, ... }                                        (returns immediately)

runner_wait_for { runId, pattern: "ready in", stream: "stdout" }
-> { outcome: "matched", endpoints: ["http://localhost:5801/"] }

# Refresh after a code change:
runner_restart { runId }
runner_wait_for { runId, pattern: "ready in" }

# Stop:
runner_kill { runId }
```

**Critical rule for services:** never call `runner_start` twice for
the same service. Use `runner_restart` to refresh under the same
`runId`. If you've lost the runId, find it with
`runner_list { state: "running", name: "fe" }`.

### 3. Instrumented scripts (optional)

If you write a script yourself, you can emit explicit
`section_start` / `section_end` / `metric` / `event` / `fail` markers
through the `runnerlog` helpers (bash sourceable, python module + CLI
shim) -- giving the runner explicit structure rather than relying on
output adapters.

```bash
source "${XDG_DATA_HOME:-$HOME/.local/share}/runner-mcp/lib/runnerlog.sh"
runnerlog_section_start build
go build ./...
runnerlog_section_end build ok exit=$?
```

You **never** write `::run::` protocol lines by hand. Always go
through a helper.

### 4. Agent collaboration (multi-turn)

Delegate a self-contained task to a peer agent and hold a real
conversation with it across turns -- it keeps context between messages,
so it's a true collaborator rather than a single-use prompt.

```
# Start a conversation (returns a runId for the sub-agent run):
research_and_code_assistant_agent { ask: "Audit src/auth for missing input validation and report findings." }
-> { runId, ... }

# Continue the SAME conversation -- the sub-agent still remembers turn 1:
research_and_code_assistant_agent { runId, ask: "Now fix the two highest-severity issues you found and run the tests." }

# Fire-and-forget while you do other work, then poll for the reply:
research_and_code_assistant_agent { ask: "...", blocking: false } -> { runId, stillRunning: true }
research_and_code_assistant_agent { runId }   # poll: returns the reply when ready
```

The sub-agent runs in the **same project** with the same file access, so
it can read, edit, build, and test alongside you. Collaboration is
backed by **opencode** today; other backends can be added later.

---

## Tools

| Tool | Purpose |
|------|---------|
| `runner_guide` | Optional deeper reference. Each other tool's description is self-sufficient; reach for the guide when a response field needs context (e.g. unfamiliar `testSummary`, `suppressedTestEvents`, `warnings`, `stillRunning`). |
| `runner_helpers` | Paths to the bash/python/CLI helpers + ready-to-paste snippets. Use when WRITING an instrumented script. |
| `runner_start` | Spawn a command (runs it exactly, never rewritten). `blocking: true` (default) waits up to ~45s, returns terminal status or `stillRunning: true`. `blocking: false` for services. Gates filter pipes + multi-step chains with a how-to message (`noScrub: true` bypasses); auto-detects known output formats. |
| `runner_restart` | Kill + respawn under the SAME runId. Use this for services -- never `runner_start` twice. |
| `runner_wait_for` | Block (~45s) until a regex matches in stdout/stderr (or run exits). Use after a non-blocking start to wait for the service ready signal. Returns `matched` / `exited` / `timeout`. |
| `runner_status` | Delta-aware status. **Auto-waits ~45s for blocking-mode runs** so the agent's protocol is just "call until terminal:true". Returns immediately for services. Surfaces `testSummary`, `endpoints`, `restartCount`, `warnings`, `stdoutTail`. Optional embedded grep. |
| `runner_section` | Drill into one section's structured detail. For go-test runs each section is a package; passing tests are filtered by default (verbose:true to see all). |
| `runner_grep` | Regex search over `stdout.log` + `stderr.log` with line numbers and `-A`/`-B` context. |
| `runner_list` | Scoreboard of all runs (global runId index). Each entry: `state`, `lastLine`, `lastLineAgeSec`, `restartCount`, `stderrCount`, `endpoints`. Filters: `state`, `name` (regex/substring). |
| `runner_kill` | SIGKILL the run's process group. |
| `runner_purge` | Remove run directories with a structured report. No args = all terminal runs in your project root. `result: "success"`/`"failed"` filters by outcome. `olderThan: N` filters by age. `runId` removes one. Active runs are never purged and are reported in `kept.active`. |
| `research_and_code_assistant_agent` | Delegate to a peer agent and hold a multi-turn conversation. `ask` (new conversation) -> `runId`; pass `runId` + `ask` to continue the same conversation; pass `runId` alone to poll for the reply. `blocking: false` to dispatch in the background. Runs in the same project with the same file access. opencode backend. |

---

## How it works (architecture)

The installer lays the payload down in the XDG data dir
(`$XDG_DATA_HOME/runner-mcp`, default `~/.local/share/runner-mcp`):

```
~/.local/share/runner-mcp/
  core/runner_core.py    # everything: CLI + library + adapters
  lib/runnerlog          # author-facing CLI shim (Python)
  lib/runnerlog.sh       # bash sourceable helpers
  lib/runnerlog.py       # python module (function API + context manager)
  mcp/dist/index.js      # MCP server (stdio transport, TypeScript -> JS)
  docs/GUIDE.md          # agent-facing guide (served by runner_guide)
  index.jsonl            # global runId -> runDir registry (no-git-repo runs)
```

The CLIs (`runner-mcp`, `runnerlog`) are symlinked into `~/.local/bin`.

### Spawn model and storage scoping

`runner_start` does a double-fork-and-setsid so the spawned process
detaches from the runner CLI and survives. PID, stdout, stderr, and
metadata go to a per-run directory.

**Storage is scoped to the AGENT'S project root**, not to the cmd's
working directory:

- If the agent's session cwd is inside a git repo, runs are stored at
  `<agent-git-root>/.runner/<runId>/`. ALL runs an agent starts -- even
  ones whose cmd targets a different project -- land here.
- Otherwise (no git root), runs go to `~/.local/share/runner-mcp/<runId>/`.

This is deliberate isolation: an agent in project A only sees its own
runs in `runner_list`, never runs that another agent in project B
started in parallel. Two agents working in the same project root WILL
see each other's runs (assumed coordinated by the engineer). The
`cwd` parameter on `runner_start` sets the SPAWN working directory for
the cmd (and may point at any path) -- it does NOT change where the
run is stored.

Run dirs are auto-excluded from the host project's `git status` via
`.git/info/exclude` (local-only -- never touches the project's
tracked `.gitignore`). The exclude entry is added the first time the
runner spawns a run inside a given git repo, then never re-added.

Each run dir contains:
- `meta.json` -- cmd (exactly as given), parser,
  cwd, pid, start/end times, exit code, state, restartCount,
  blockingMode
- `stdout.log` -- raw stdout (parsed on demand for `::run::`
  markers AND fed through registered output adapters)
- `stderr.log` -- raw stderr
- `tracker.json` -- per-agent delta cursors

### MCP server

`mcp/src/index.ts` is a thin TypeScript stdio MCP server that
translates each MCP tool call into a `runner_core.py` subcommand
invocation and returns the JSON response verbatim. There's no state
in the MCP server itself -- everything lives in the run dirs and
the global index.

### Guide is optional

`runner_guide` returns the markdown guide on demand but is not gated
on. Each other tool's description is written to be self-sufficient --
explaining its semantics, response shape, and which other tools to
chain with -- so an agent can use any tool cold. The guide is for
deeper reference when a response field surprises the agent or a more
complex workflow needs context.

### Output adapters

When stdout has no `::run::` markers in the first ~60 lines, registered
output adapters get a chance to recognize the format. The `go-test`
adapter ships today; the architecture is open for `pytest`, `jest`,
`cargo test`, etc. An adapter sniffs early lines, then synthesizes
`section_start` / `event` / `metric` / `section_end` events for the
rest of the system to consume. `runner_status.parserUsed`
tells the agent which adapter (if any) built the structure.

### Command gating (safety + correct usage)

`runner_start` **never rewrites your command** -- it runs exactly what
you give it, or it runs nothing. Before spawning, it inspects the cmd
(without modifying it) and **gates** two patterns, returning a positive,
instructional message instead of executing:

1. **Trailing filter/pager pipes** (`| grep`, `| tail`, `| head`, `| wc`,
   `| less`, ...). The runner already captures full output and gives you
   `runner_grep` / `runner_section` / the auto `stdoutTail`, so the pipe
   is unnecessary and hides output from the runner's adapters. The gate
   tells you to re-run the producer command alone and filter the captured
   output.
2. **Multi-step chains** (`&&` / `||` / `;` joining 2+ commands). These
   lose per-step visibility and force fragile shell escaping. The gate
   points you at the `.runner/scripts/` workflow (below).

Why gate instead of silently stripping pipes (the old behavior)? Rewriting
a command and then executing the rewrite is unsafe -- a mis-parse could
turn a benign-looking command into a destructive one you never wrote. The
runner refuses to run a command it would have to alter, and instead shows
you the correct form.

Pass `noScrub: true` to bypass the gate and run the exact string verbatim.

### `.runner/scripts/` -- the home for multi-step runner scripts

For anything beyond a single producer command, write a small script in your
project's `.runner/scripts/` directory and run that. This directory:

- is **already git-excluded** (it's under `.runner/`), so scripts never
  show up as project noise;
- lives next to your run data, so scripts are easy to find, copy, reuse,
  and enhance;
- is the **productive alternative to escaping a compound one-liner** -- you
  write normal shell, and with the `runnerlog` helpers each step reports
  structured status (`section_start` / `section_end` / `metric` / `event`),
  which the runner surfaces as `failedSections`, per-section timing, and
  metrics.

Call `runner_helpers` for ready-to-paste bash/python/CLI instrumentation
snippets and the exact paths, then:

```sh
runner_start { cmd: "bash .runner/scripts/task.sh" }
```

### Endpoint detection

After every run completes (or whenever `runner_status` / `runner_list`
runs), the first ~200 lines of stdout+stderr are scanned for service-up
patterns -- vite "Local: http://...", go "listening on :PORT" / `addr:
":PORT"`, express "Server running on port N", uvicorn "Uvicorn running
on http://...", rails "Listening on http://...". Detected URLs/ports
are surfaced in `endpoints` so the agent never has to grep "what port
is this on?".

### Warning detection

After every terminal run, stdout+stderr are scanned (last 1 MB each,
bounded) for `ERROR` / `FAIL` / `panic:` / `fatal` / `Traceback ` /
`connection refused` patterns. If any match, the response includes:

```json
"warnings": {
  "count": 6,
  "sample": [{ "stream": "stdout", "lineNo": 91, "line": "ERROR: ..." }],
  "hint": "Output contains ERROR / FAIL / panic / fatal lines even though exit code may be 0..."
}
```

This catches the common case where a script (e.g. `make reinstall`
with optional seeding) reports `exitCode: 0` while individual steps
logged real errors.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Tool description references concept I don't recognize | Call `runner_guide` for the full reference; e.g. for `testSummary`, `suppressedTestEvents`, `warnings`, `blockingWaitSec`, `parserUsed`. |
| Agent loses runId, starts duplicate services | Use `runner_list { state: "running", name: "..." }` to find runs. Use `runner_restart` (not `runner_start`) to refresh. |
| MCP transport timeout (~60s) on `runner_start` | Should not happen with the fixed ~45s `BLOCKING_WAIT_SEC`. If it does, the run is still alive -- call `runner_status { runId }` with the id you got back. |
| `result: "success"` but build broke | Check `warnings` field. Many shell scripts exit 0 while logging errors. |
| `runner_status { runId }` says "not found" | The global runId index (`~/.local/share/runner-mcp/index.jsonl`) might be missing the entry; pass `cwd` to fall back to walking standard run roots. |
| `stdoutTail` shows nothing useful | Run was an adapter run (e.g. go test). Use `runner_section { name: "<package>" }` instead. |
| `runner-mcp` / `runnerlog: command not found` | `~/.local/bin` not on `$PATH`. Add `export PATH="$HOME/.local/bin:$PATH"` to your shell rc. |
| Installer says `node` / `python3` missing | Install Node and Python 3, then re-run the installer. Both are runtime requirements. |
| Agent doesn't auto-register | The installer auto-registers with opencode/Claude/Codex/Grok/VS Code only if their config/CLI is present. opencode and VS Code auto-registration also need `jq`. |

---

## Layout (source)

```
runner-mcp/
  install.sh                # the installer (curl | sh)
  Makefile                  # build / install / release
  VERSION                   # single source of truth for the version
  LICENSE                   # MIT
  README.md                 # this file
  core/
    runner_core.py          # core implementation: CLI, adapters, library
  lib/
    runnerlog               # author-facing CLI shim (Python)
    runnerlog.sh            # bash sourceable helpers
    runnerlog.py            # python module + context manager
  mcp/
    src/index.ts            # MCP server (stdio transport)
    package.json
    tsconfig.json
  docs/
    GUIDE.md                # agent-facing guide (served by runner_guide)
  tui/
    main.go                 # runner-mcp TUI (live run dashboard)
```

---

## Conventions

- Agents address runs by `runId` only -- the global
  `~/.local/share/runner-mcp/index.jsonl` resolves any runId to its run
  dir, so `cwd` is **not** required on follow-up tool calls.
- Service runs should be given a memorable `name` (e.g. `"fe"`,
  `"api"`) so they're easy to find with `runner_list`.
- Filter pipes (`| head`, `| tail`, `| grep`, etc.) belong to the
  runner, not to the spawned cmd. Use `runner_grep` / `runner_section`
  / the auto `stdoutTail` instead.
- Test commands should NOT shell out and grep -- the go-test adapter
  surfaces failures structurally.

---

## License

MIT. See [LICENSE](LICENSE).
