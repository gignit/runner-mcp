# Runner -- Agent Usage Guide

You are an AI agent reading the runner reference. The `runner` MCP
gives you structured control over shell commands -- one-shot
test/build runs, long-running services, and instrumented scripts --
WITHOUT grepping logs or losing track of background processes.

**This guide is OPTIONAL.** Each runner tool's description is
self-sufficient: it explains what the tool does, what response shape
you'll get back, and which other tools to chain with. Use this guide
when:

- A response field surprised you (testSummary, suppressedTestEvents,
  parserUsed, warnings, stillRunning, blockingWaitSec).
- You're not sure which workflow applies to your task.
- You want the full anti-pattern list before doing something risky.
- A response told you to consult the guide.

Skim, don't memorize. Drop in when needed.

## The Four Workflows

The runner is built for four distinct use cases. Understand which one
you're in before calling tools.

### 1. One-shot builds and tests (blocking, default)

For commands that should complete and exit: `make reinstall`, `npm run
build`, `go test`, `cargo build`, `terraform apply`, `pytest`, lint,
type-check, migrations, etc.

```
runner_start { cmd: "make reinstall", cwd: "/path/to/project" }
runner_start { cmd: "go test ./...", cwd: "/path/to/project" }
runner_start { cmd: "npm run build", cwd: "/path/to/project" }
```

**Use this instead of bash for non-trivial commands.** It gives you:

- `stdout` (and `stderr`) -- line-numbered output. When the run is
  small enough, you get the FULL stream (every line, prefixed with its
  file lineNo like `   42: <text>`). When the run is large, you get
  the head + a literal `... <N lines omitted; lines X..Y> ...` marker
  + the tail, all line-numbered. **Never trust a tail-only view to
  show you the operation's own output -- if you wrote `cmd && tail
  log` in your cmd, your operation's output may be in the head or
  middle, NOT the tail.** The line numbers and the gap marker make
  the structure obvious; use `runner_grep` or `runner_status { since:
  N }` to fetch any elided range.
- `warnings` -- count + sample of ERROR / FAIL / panic / fatal /
  "connection refused" lines, even when exit code is 0. Many builds
  (e.g. `make reinstall` with optional seeding) report success at the
  make level while individual steps logged real errors. The runner
  spots these so you don't trust a green result blindly.
- A `runId` you can re-query later -- no re-running the command to
  see what it printed.
- For `go test`: per-package `testSummary` with `failedTests` +
  `nextCalls` (machine-readable next-step actions). See "Output
  adapters" below.

**Filter pipes are gated, not run.** If your cmd ends in `| head`, `| tail`,
`| grep`, `| awk`, `| sed`, `| wc`, etc., `runner_start` refuses to run and
returns a how-to message (it never rewrites your command). Re-run the
producer command alone -- the runner captures the full output and has
structured filtering tools, so the pipe is unnecessary:
- Want to find a specific pattern? Call `runner_grep { runId, pattern }`.
- Want a specific section's events? Call `runner_section { runId, name }`.
- Want a specific line range? Call `runner_status { runId, since: N }`.

**When NOT to use runner_start:**
- Trivial one-liners like `echo`, `ls`, `cat /etc/hosts`, single
  `which foo` checks. The runner adds spawn overhead and a runId
  you'll never use. Reach for bash directly.
- Pure shell scripting that's part of a larger flow you're composing
  yourself (e.g. building a JSON payload from multiple sources before
  sending it). Runner doesn't help there.

**When TO use runner_start:**
- Builds, tests, installs, migrations, deploys -- any command that
  should complete and report a meaningful result.
- Multi-step pipelines (loops, `&&` chains, `for ... do ... done`)
  where you want one tracked operation. Loops are NOT pipes; they
  are NOT scrubbed.
- Anything you'd want to inspect later by `runId` instead of running
  again.

`blocking: true` is the default. The call holds open up to a fixed
~45-second wait, then returns. **The wait is NOT user-tunable** -- the
MCP transport caps every request at ~60s and you can't raise that, so
exposing a knob would be a footgun. **The job is never killed by the
wait.**

**The protocol for long-running blocking runs is dead simple:**

1. Call `runner_start`. If it returns `terminal: true`, you're done.
2. If it returns `stillRunning: true`, just call `runner_status { runId }`.
3. `runner_status` AUTO-WAITS another ~9 min for blocking-mode runs.
   Either it returns terminal status or it returns `stillRunning: true`
   again with the same followUp.
4. Repeat step 3 until `terminal: true`. The runner handles all the
   timing -- you just keep calling `runner_status` with the same runId.

For a 5-minute build, that's ~7 `runner_status` calls. No sleeps, no
polling cadence to figure out, no risk of polling too aggressively.

This auto-wait only applies to BLOCKING-MODE runs (the default). For
services started with `blocking: false`, `runner_status` returns
immediately -- you don't want to block on a run that's designed to
keep running indefinitely.

For `go test`, the runner auto-detects the output format and
synthesizes per-package sections with per-test results. A typical
`go test ./... -v` response (250 tests, 2 failing) carries ~10 events
in `delta.newEvents` and a compact `testSummary`, not 250 event
objects -- passing tests are suppressed (counted in
`delta.suppressedTestEvents`) so the response stays small. Pass
`verbose: true` if you really want every test event.

### 2. Long-running services

For dev servers, watchers, REPLs, file pollers -- anything that runs
indefinitely. The full lifecycle:

```
# 1. Start (returns immediately):
runner_start { cmd: "npm run dev", name: "frontend-dev", blocking: false }
-> { runId, pid, runRoot, ... }

# 2. Wait until actually ready (don't sleep + curl, use this):
runner_wait_for { runId, pattern: "ready in", stream: "stdout" }
-> { outcome: "matched", match: {...}, endpoints: ["http://localhost:5801/"] }
# Wait is fixed at ~9 min. If outcome="timeout", run is still alive --
# just call runner_wait_for again to keep waiting.

# 3. See what's running across the board:
runner_list { state: "running" }
-> [{ name, runId, lastLine, lastLineAgeSec, endpoints, restartCount, ... }]

# 4. Refresh after a code change to the server:
runner_restart { runId }
-> { runId (same), pid (new), restartCount: 1, killedPreviousPid: true }
runner_wait_for { runId, pattern: "listening on", stream: "stderr" }

# 5. Stop:
runner_kill { runId }
```

**Wait for ready, don't guess.** After `runner_start { blocking: false }`,
the process exists but the service isn't bound yet. Use
`runner_wait_for` with a regex matching the framework's ready signal:

| Framework | pattern | stream |
|-----------|---------|--------|
| vite      | `ready in` | stdout |
| next.js   | `ready started server` | stdout |
| go (zap)  | `server starting\|listening on` | stderr |
| express   | `Server running on port` | stdout |
| uvicorn   | `Application startup complete` | stderr |
| rails     | `Listening on http` | stdout |

`runner_wait_for` returns one of three outcomes:
- `matched` -- pattern found; bonus `endpoints` list
- `exited` -- service crashed before becoming ready; includes `stderrTail`
- `timeout` -- still alive but pattern not seen; agent decides what next

The job is **never killed** by `runner_wait_for`.

**Critical rule: NEVER call `runner_start` twice for the same service.**
Agents commonly fail at this -- they start `npm run dev`, lose the
runId, then start it again to "refresh", ending up with two servers
fighting for port 3000. The fix:

- Give services a memorable `name` so you can find them with `runner_list { name: "..." }`
- Use `runner_restart` to refresh -- same runId, fresh process
- Use `runner_kill` to stop -- same runId

**Use `runner_list` as your scoreboard.** Every entry includes
`endpoints` (URLs/ports parsed from logs), `lastLine` + `lastLineAgeSec`
(staleness signal), `restartCount`, and `stderrCount`. One call
answers "what's running and what port?". Filter by `name` (regex or
substring) when you have many services.

### 3. Instrumented scripts

Scripts you write that emit explicit `section_start`/`section_end`/
`metric`/`event`/`fail` markers via the `runnerlog` helpers (bash,
python, or CLI). See "Writing instrumented scripts" below.

### 4. Delegating to a peer coding agent

You can hand a self-contained task to another coding agent (opencode
today; the backend is pluggable) and get its reply back as a
structured response. The sub-agent runs in the same project root with
the same file access you have -- it's a peer, not a sandboxed
assistant.

**One tool, two modes:**

- **`ask` set** = send a prompt. With no `runId`, starts a new
  conversation. With a `runId` (UUIDv7), continues that conversation.
  With a `runId` that's a backend session id (e.g. `ses_...`), adopts
  that external session.
- **`ask` omitted, `runId` set** = poll for the response on an
  existing turn. "Is it done yet? Give me the answer when it is."

That's it. No `runner_wait_for`, no `runner_status` for sub-agents.
The dispatch tool is the polling tool.

Optional: `blocking: false` to fire-and-forget. Useful for parallel
dispatches (kick off N sub-agents, do other work, poll later) or for
investigations you expect to take many minutes.

```
# Send: short turn returns the answer directly.
research_and_code_assistant_agent { ask: "Reply with one word: alpha." }
-> { runId, terminal: true,
     finalReply: { text: "alpha", totalToolCalls: 0, totalTokens: 144913 } }

# Send: long turn returns stillRunning after the ~9 min wait window.
research_and_code_assistant_agent { ask: "Review pkg/auth for race conditions..." }
-> { runId, terminal: false, stillRunning: true,
     agent: { turnDurationSec: 45, ... currentActivity?, recentToolCalls? },
     followUp: "Call research_and_code_assistant_agent with just runId='...' to keep waiting." }

# Fire-and-forget: spawn in background, return runId immediately.
research_and_code_assistant_agent {
  ask: "Investigate src/auth for race conditions, take your time, be thorough.",
  blocking: false
}
-> { runId, blocking: false, stillRunning: true,
     followUp: "...Call research_and_code_assistant_agent with just runId=... to poll..." }
# Now do other work, dispatch other agents in parallel, etc.

# Poll: just pass runId. Blocks ~9 min until terminal, returns finalReply when ready.
research_and_code_assistant_agent { runId: "019e..." }
-> { runId, terminal: true,
     finalReply: { text: "...the complete reply...", totalToolCalls: 6, ... } }

# Repeat poll if still not done -- same call, same shape, until terminal:true.

# Continue: send a follow-up to the same conversation.
research_and_code_assistant_agent { runId, ask: "Now check JWT verification." }
-> { runId (same), agent: { turn: 2 }, finalReply: { text: ... } }

# Interrupt + redirect: send a new ask while the sub-agent is in flight.
# The current turn is killed (SIGKILL to its process group); a fresh
# opencode is respawned with the SAME backend session id, so the
# sub-agent sees the full prior conversation context plus your new
# instruction -- exactly like a human pressing Escape in a TUI and
# typing a new message.
research_and_code_assistant_agent {
  runId,
  ask: "Stop what you were doing. Forget the JWT path; instead audit pkg/db for connection leaks."
}
-> { runId (same), agent: { turn: 3 }, finalReply: { text: "...redirected investigation..." } }
```

**Key properties:**

- **runId-keyed**, just like every other runner tool. You never need
  to think about the backend's internal session id -- runner captures
  it from the sub-agent's event stream and reuses it transparently on
  follow-up turns.
- **Conversation persists across turns**. The sub-agent remembers
  prior turns within the same runId. Each new turn replaces the
  visible transcript -- only the most recent turn's output is shown
  -- but the sub-agent's own context is intact.
- **Terminal response: focused `finalReply` only.** When the turn
  finishes, the response carries the sub-agent's actual answer --
  nothing else:
  ```jsonc
  "finalReply": {
    "text": "...the sub-agent's complete reply text...",
    "totalToolCalls": 182,       // how many tools ran across the turn
    "totalTokens": 722886,
    "recentToolCalls": [          // last 8 tools, for context
      "[tool: chrome-devtools_take_screenshot] -- ... (completed)",
      "..."
    ]
  }
  ```
  No tool input/output bodies, no replay of every step. The size of
  `finalReply.text` is bounded by what the sub-agent actually wrote
  as its conclusion (typically 1-10 KB regardless of how many tools
  it ran). A 182-tool marathon turn produces the same ~7 KB
  response as a 4-tool quick turn.
- **In-flight response: progress only.** When a turn is still
  running after the ~9 min wait, no `finalReply` yet -- the response
  surfaces `agent.currentActivity` (most recent completed tool +
  status + when it ended), `agent.recentToolCalls` (last 5 tool
  one-liners), `agent.tokensSoFar`, and `agent.turnDurationSec`.
  Plus the `followUp` hint telling you to call this same tool with
  just `runId` to keep waiting.
- **Interrupted response: recoverable failure.** Sub-agent backends
  occasionally hit transient errors (rate limits, API outages, etc.)
  and exit without completing the turn. Response:
  ```jsonc
  {
    "terminal": true,
    "result": "interrupted",
    "agent": {
      "interrupted": true,
      "interruptReason": "Rate limited",
      "interruptCode": "APIError",
      "interruptKind": "rate_limit_error"
    },
    "followUp": "...dispatch another turn on the SAME runId with ask='continue'..."
  }
  ```
  The backend session is preserved. To resume, send a follow-up
  turn on the same runId with `ask: "continue"` (or any prompt) --
  the sub-agent picks up where it left off. **Do not poll an
  interrupted turn for a reply; there isn't one. Send a new turn
  instead.**
- **Note on `currentActivity` timing.** Opencode emits tool events
  when each tool COMPLETES (not when it starts). So
  `currentActivity` is the most recently *completed* tool, not
  what's running right now. Use `startedAtMs` to gauge staleness --
  a fresh timestamp means the tool just finished and the sub-agent
  is now thinking or starting the next one.
- **Logs are append-only across the entire conversation.** Both
  `stdout.log` (conversational text the sub-agent emitted) and
  `stderr.log` (structured NDJSON event stream) keep accumulating
  across every turn -- runner does NOT wipe them at turn
  boundaries. You can `tail -f` the run dir and watch the agent
  talk in real time without ever restarting the tail. Per-turn
  start positions are recorded in `meta.agentTurnCursors[]` (line
  + byte offsets, both stdout and stderr) so structured tooling
  can scope to a specific turn.
- **`runner_grep` is turn-scoped by default for sub-agent runs.**
  Because the logs are append-only, a naive grep would mix matches
  from every turn. By default `runner_grep` on a sub-agent run
  searches ONLY the current turn's slice and adds
  `scope: "current-turn"` to the response so you know. Pass
  `allTurns: true` to search the whole conversation -- each match
  then carries a `turn: N` field so you can place it. Use the
  all-turns mode when you need to trace back to an earlier turn's
  tool call or text.
- **Full transcript stays on disk.** The complete rendered
  conversation text is at the run's `stdout.log`, the full
  structured NDJSON event stream (every tool call's input and
  output, every step_finish, every step_start, every error) is at
  `stderr.log`. The agent never has to read these files -- the
  `finalReply` / `progress` blocks in the MCP response give you
  what you need 99% of the time. But when you need to drill in,
  they're always there.
- **Sub-agent runs are normal runs**. `runner_list` shows them with
  an `agent: { runtime, turn, backendSessionId }` block so you can
  find a prior conversation to continue.
- **Interrupting a sub-agent works like Escape-then-type in a TUI.**
  To redirect a sub-agent mid-flight, just dispatch a new `ask` on
  the same runId. Runner SIGKILLs the running turn's process group,
  archives the old prompt to `prompts/<N>.md`, writes the new prompt,
  respawns opencode with `-s <session_id>`, and the sub-agent resumes
  with full prior context plus your new instruction. No separate
  kill call needed -- the dispatch handles it.
- **`runner_kill` cancels but does NOT end the conversation.** The
  backend session id is preserved in meta.json. The kill response
  includes `conversationPreserved: true` and a `resumeHint` showing
  the exact `research_and_code_assistant_agent` call to dispatch a
  new turn. Resume any time -- the sub-agent's memory is intact.
- **Runner auto-kills wedged sub-agents.** If runner detects the
  turn is interrupted (rate limit, API error, backend stderr
  signaling a failure) but the opencode process is still spinning
  on retries, runner SIGKILLs the wedged process group on the next
  poll. The response carries the standard `agent.interrupted` block
  and the followUp telling you to send `continue` -- no quota waste
  from doomed retries.

**When to use this:**

- You want a second opinion on a design or PR.
- You want a parallel investigation while you focus on the main
  thread (e.g. "go figure out why these tests are flaky" while you
  keep working).
- You're about to do a self-contained subtask that's easier to scope
  out: "write a markdown summary of every TODO in src/" -- delegate
  it.
- You want a fresh-context agent to review your work without your
  context window's biases.

**When NOT:**

- Anything you can do faster yourself in 1-2 tool calls.
- Operations that need YOUR session's working state (open files, in-
  progress edits). The sub-agent gets the filesystem, not your
  conversation context.

**Adopting an existing backend session.** Sometimes the operator
hands you a backend session id (e.g. an opencode `ses_...`) for a
conversation that already exists OUTSIDE runner -- they may have
started it interactively in a terminal, or another tool created it.
You want to engage with that ongoing conversation, not start a new
one.

Pass the backend session id as `runId`:

```
research_and_code_assistant_agent { runId: "ses_1cf0fa554ffeJOd1LTNdDSqsff",
                                    ask: "Continue where we left off..." }
-> { runId: "019e3834-10b0-7d7b-8b84-a65ea3be4ec3",    # NEW runner runId
     agent: { runtime: "opencode", turn: 1,
              backendSessionId: "ses_1cf0fa554ffeJOd1LTNdDSqsff" },
     finalReply: { text: "...sub-agent's reply, with full memory of prior turns..." } }
```

Runner recognizes that the id isn't a runner-issued runId (UUIDv7
shape), so it scaffolds a wrapper around the backend session, spawns
the backend with that session id, and returns a real runner runId.
**Use the runner runId for every subsequent turn** -- the backend
session id becomes invisible from then on, just like any other
sub-agent conversation.

Three things to know:

- If the backend rejects the session id (it doesn't exist or you
  typo'd it), the sub-agent's error message lands in the run's
  `stderr` exactly as the backend wrote it (e.g. opencode emits
  `Session not found: ses_...`). Read the transcript; runner does
  not try to interpret it for you.
- If runner already wraps that backend session (you previously
  adopted it, or it was started via runner), you get a structured
  error pointing at the existing runner runId. Use that one to
  continue -- never two runner runs wrapping the same backend
  session, since each turn would wipe the other's transcript.
- Adopted runs show up in `runner_list` with the name
  `opencode-adopted-<N>` (vs `opencode-agent-<N>` for fresh
  conversations), so you can tell at a glance which conversations
  were imported.

The conversation is one-turn-at-a-time: each call to the tool blocks
until the sub-agent's turn finishes or ~9 min elapses. If
`stillRunning: true` comes back, call this same tool with just
`runId` (no `ask`) to keep waiting -- same protocol, same response
shape, until `terminal: true`.

---

## Dispatching sub-agents effectively

This section is the field-tested playbook for using
`research_and_code_assistant_agent` on real code-touching work --
code reviews, refactors, layer audits, etc. Skim it before your first
serious dispatch.

### The spec contract (every code-touching dispatch)

A sub-agent will believe the spec you give it. Vague specs produce
confident-but-wrong work. Every `ask` that asks the sub-agent to TOUCH
code (not just read it) should include all of:

- **Pre-checks via your code-intelligence tools** -- tell the
  sub-agent to verify the symbol/file/dir exists before editing.
  STOP-and-report if anything is missing rather than improvising.
- **A reference pattern in existing code** -- "follow the shape of
  `src/foo/bar.go::Doer`". Verify that pattern actually exists
  before sending the spec.
- **PROJECT_REQUIREMENTS section refs** when the project has one
  ("see §5.3"). Anchors the work to a fixed source of truth instead
  of the sub-agent's interpretation.
- **Explicit DO-NOTs.** Sub-agents over-reach. "Do not edit anything
  outside `pkg/auth/`. Do not regenerate go.mod."
- **Deliverable contract.** Exactly what files change, what tests
  must pass, what the response should include. End with "re-verify
  every claim before responding."
- **A quality bar.** "Don't accept anything except perfection" is
  real -- sub-agents otherwise return half-done work with confident
  prose.

The runner doesn't enforce any of this; it's just what works.

### When to reuse runId vs start fresh

- **Reuse the runId** for: following up on a peer-review finding,
  resuming after a long pause, iterating on the same artifact, asking
  clarifications. The sub-agent's context is already warm and you
  save the token cost of re-establishing it.
- **Fresh runId** for: an unrelated task, a fresh-context second
  opinion (you specifically want the bias of the prior conversation
  GONE), or when the prior conversation got derailed and re-anchoring
  would cost more than starting over.

### Parallel dispatch

You CAN dispatch two sub-agents at once -- they run in their own
processes. Use `blocking: false` on each dispatch so the calls return
immediately with the runId, then poll each one later:

```
# Fire off three investigations in parallel.
research_and_code_assistant_agent { ask: "Audit pkg/auth...", blocking: false }
   -> { runId: "019e...auth" }
research_and_code_assistant_agent { ask: "Audit pkg/db...",   blocking: false }
   -> { runId: "019e...db" }
research_and_code_assistant_agent { ask: "Audit pkg/net...",  blocking: false }
   -> { runId: "019e...net" }

# Now do other work, then come back to collect.
research_and_code_assistant_agent { runId: "019e...auth" }   # blocks until done
research_and_code_assistant_agent { runId: "019e...db" }
research_and_code_assistant_agent { runId: "019e...net" }
```

Caveats:

- **Audit file targets before sending.** If the two specs touch any
  shared file, serialize them with explicit handoff context instead
  ("agent A finished pkg/x; now you do pkg/y, here's what A
  changed"). Parallel agents writing to the same file is a merge
  hazard the runner does not protect against.
- **Each parallel agent gets its own runId.** Track them; use
  `runner_list` to monitor.
- **`blocking: true` is fine for parallel too**, but the calling
  agent's MCP transport holds the response for each call up to
  ~9 min, so you can't do real parallelism that way -- the calls
  serialize. Use `blocking: false` to actually parallelize.

### Peer-review gate (before commit)

Sub-agents over-claim test coverage and over-claim deletions. Always
verify before merging their work:

1. `runner_start { cmd: "go build ./... && go test ./..." }` --
   structured per-package pass/fail. Don't trust the sub-agent's
   "all tests pass" report; trust the adapter output.
2. A code search (your code-intelligence tools, or `runner_grep` on
   a relevant build log) for every concrete claim the sub-agent made
   about deletions, migrations, or renames. Confirm the change
   actually happened where it said it did.
3. A dependency/layer audit when the sub-agent claimed to fix import
   structure.
4. `make reinstall` (or your equivalent) + a baseline smoke command
   for any change that affects live behavior.

### Compaction awareness

Long sub-agent conversations get internally compacted by the backend
(opencode compresses prior turns to stay within the context window).
Compaction preserves architectural intent but **loses mechanical
specifics** -- exact line numbers, variable names, exact strings the
prior turn used.

**Detecting compaction.** Each `step_finish` event in stderr.log
carries `part.tokens.total` and `part.tokens.cache.read`. A sharp
DROP in `tokens.total` between turns combined with a SPIKE in
`cache.read` is compaction. Watch for it with `logwatch`:

```
logwatch --file .runner/<runId>/stderr.log \
  --grep '"type":"step_finish"' \
  --jq '{tokens: .part.tokens.total, cacheRead: .part.tokens.cache.read}' \
  --last 50
```

**After compaction, the next dispatch on the same runId MUST:**
- Re-state specific code locations (file:line, exact symbol names).
- Re-anchor the goal in plain language.
- Tell the sub-agent to verify pre-existing state (with whatever
  code-intelligence tools it has) BEFORE editing -- it no longer
  remembers the exact state it left things in.

### Token + duration monitoring (and what NOT to do)

Modern long-context models (opus 4.7 et al.) stay coherent well past
half a million tokens. **Do not kill a sub-agent just because the
turn is slow or token counts are high.**

| Signal | Meaning | Action |
|---|---|---|
| Rising token count | Sub-agent is actively working | Wait |
| Flat `lastTokens` between polls | Slow LLM response OR active tool call | Check `stderrNewCount` for activity; wait |
| `lastTokens` drops sharply + `cacheRead` spikes | Backend compacted | Continue, but re-anchor next turn |
| Provider terminal error | Real failure | `runner_kill`, fix the spec, dispatch again |

**Kill when:**
- The provider returned a terminal error (auth, quota, model-not-found).
- You realized the spec was wrong and the turn would waste real time.
- A concurrent agent is about to collide on the same files.

**Don't kill for:**
- High token counts.
- A turn that has been quiet for a few minutes.
- Impatience.

**Note: kill does NOT end the conversation for sub-agent runs.** The
backend session id is preserved; you can resume any time with a
`research_and_code_assistant_agent { runId, ask: "..." }` dispatch.
The kill response carries an explicit `resumeHint` showing the exact
call. Often you don't even need to kill explicitly -- sending a new
`ask` on the runId auto-kills the in-flight turn before respawning,
which is the right idiom for "stop what you're doing and do this
instead." Use `runner_kill` only when you want the turn dead RIGHT
NOW (e.g. before file-collision damage) and don't have the next ask
ready yet.

### Monitoring without burning context: logwatch + jq

Polling via `runner_status` every few seconds works but **burns the
caller's context window** with full status JSON each time. Use
`logwatch` instead -- it tails files with persistent resume cursors
and prints only what's new.

Track sub-agent activity (high signal, low noise):
```
logwatch --file .runner/<runId>/stdout.log \
  --grep 'step-finish|tool: bash|FAIL:' \
  --last 10 --duration 30
```

See raw tool inputs in full (untruncated commands the sub-agent ran
-- stderr is where NDJSON lives):
```
logwatch --file .runner/<runId>/stderr.log \
  --grep '"tool":"bash"' \
  --jq '{desc: .part.state.input.description, cmd: (.part.state.input.command // "" | .[0:120])}' \
  --last 20 --duration 5
```

Watch multiple agents at once (shell brace expansion):
```
logwatch --file .runner/{<id1>,<id2>}/stdout.log \
  --grep 'step-finish|FAIL:' --duration 60
```

**Cursor gotchas:**
- Each unique `--grep` string has its own persistent cursor per file.
- After a `--last N` invocation, rerun WITHOUT `--last` to advance
  the cursor normally from where you stopped.
- `-A N` / `-B N` give context lines around each match.

**Anti-pattern: polling `runner_status` every few seconds.** Each
call dumps the full status response into your context. For active
monitoring use `logwatch --duration N`; for one-shot "is it done
yet?" use `runner_status`.

---

## Tool Reference

| Tool | Purpose |
|------|---------|
| `runner_guide` | Returns this guide (optional reference). Call when an unfamiliar response field, workflow, or anti-pattern needs context. Other tools' descriptions are self-sufficient. |
| `runner_helpers` | Returns paths to bash/python/CLI helpers + ready-to-paste snippets. Call when WRITING an instrumented script. |
| `runner_start` | Spawn a command. Default `blocking: true` (waits for terminal or timeout). Set `blocking: false` for services. |
| `runner_restart` | Kill + respawn under the SAME runId. Use this for services -- never `runner_start` twice. |
| `runner_wait_for` | Block until a regex matches in stdout/stderr (or run exits or timeout). Use after `runner_start { blocking: false }` to wait for a service "ready" signal. Not for sub-agent runs -- use `research_and_code_assistant_agent` with just runId for that. |
| `runner_status` | Delta-aware status check. AUTO-WAITS ~9 min for blocking-mode runs (so the agent's protocol is just "call until terminal:true"). Returns immediately for services. Surfaces `testSummary`, `endpoints`, `restartCount`. Optional `grep` for embedded regex search. |
| `runner_section` | Drill into one section's full detail. For go-test runs, the section is a package and contains failed-test events + assertion lines. |
| `runner_grep` | Regex search over stdout.log + stderr.log. |
| `runner_list` | Scoreboard of all runs with `lastLine`, `endpoints`, `restartCount`, `stderrCount`. Filter by `state` and `name`. Use this to find what's running. |
| `runner_kill` | SIGKILL the run's process group. Works on sub-agent turns too. |
| `runner_purge` | Remove run directories. No args -> all terminal runs in your project root. `result: "success"` or `"failed"` -> only those. `olderThan: N` -> terminal runs older than N sec. `runId: <id>` -> just that one. Active runs are NEVER purged; they show up in `kept.active`. |
| `research_and_code_assistant_agent` | Delegate a task to a peer coding agent. Two modes: pass `ask` to SEND (new conversation or continuation, runId optional); pass only `runId` to POLL for the response on an existing turn. Returns `finalReply.text` when the turn completes -- no transcript dump. Full transcript on disk at the run's `stdout.log` (use `runner_grep` if needed). See workflow #4 above. |

---

## Command gating

The runner runs your `cmd` **exactly as written** -- it never rewrites it.
Instead, it inspects the command and **refuses to run** (returning a how-to
message) for two patterns, so you re-issue them the productive way:

1. **Trailing filter/pager pipes** -- `| head | tail | grep | egrep | fgrep |
   rg | ack | less | more | wc | cat | awk | sed | cut | sort | uniq | tee |
   column | fold | fmt | tr`. You don't need these: the runner captures full
   output. Run the producer command alone, then filter with `runner_grep` /
   `runner_section` / the auto `stdoutTail`.
2. **Multi-step chains** -- `&&` / `||` / `;` joining 2+ commands. Write a
   script instead (see below); you get per-step structured status and avoid
   fragile escaping.

Why gating (not silent stripping)? Rewriting a command and executing the
rewrite is unsafe -- a parse mistake could run something destructive you
never wrote. So the runner runs your exact command or none at all.

Pass `noScrub: true` to bypass the gate and run the exact string verbatim.

### Multi-step work -> `.runner/scripts/`

For compound jobs, write a small script in your project's `.runner/scripts/`
directory (already git-excluded -- no project noise) and run it:

```
runner_start { cmd: "bash .runner/scripts/task.sh" }
```

Instrument it with the `runnerlog` helpers (call `runner_helpers` for
ready-to-paste snippets) so each step reports `section_start` / `section_end`
/ `metric` / `event`, which the runner surfaces as `failedSections`,
per-section timing, and metrics. This is far more productive and reliable
than trying to escape a long `&&` one-liner.

---

## Output adapters: structured tests without instrumentation

When you run an uninstrumented test command, the runner sniffs early
output and engages an adapter that synthesizes the same structured
events your script would emit if it called the runnerlog helpers.

### go-test adapter (auto)

```
runner_start { cmd: "go test ./...", cwd: "/path" }
```

The adapter parses go test output into one section per package and the
following events (parsed -- see "what the agent sees" below for what
actually surfaces in the response):

- `section_start <package-path>`
- One `event` per test (`PASS TestX (0.42s)` / `FAIL TestY (0.10s)` /
  `SKIP TestZ (0.00s)`)
- For each FAIL: the captured `<file>.go:<line>: <message>` assertion
  lines as additional events tagged `kind: failure-detail`
- A `metric` carrying `{ testsPass, testsFail, testsSkip, elapsedSec }`
  -- if `go test -v` was NOT used, per-test counts are unavailable and
  the metric carries `perTestCounts: "unavailable (run with -v ...)"`
  instead.
- `section_end <package-path> ok|failed reason="..."` with reasons
  including `"test failures"`, `"build failed"`, `"no test files"`

**What the agent sees in `runner_status` / blocking response:**

```
result: "failed"
sectionsFailed: 1
failedSections: ["example/pkg/store"]
recentlyChangedSections: [
  { name: "example/pkg/store", status: "failed", reason: "test failures",
    metrics: { testsPass: 14, testsFail: 1, testsSkip: 0, elapsedSec: 1.234 } },
  ...
]
parserUsed: "go-test"
testSummary: {
  status: "failed",                     // "failed" | "all_passed" | "packages_ok" | "no_tests"
                                        //   failed       = at least one package failed
                                        //   all_passed   = per-test data + everything green (used `-v`)
                                        //   packages_ok  = packages all green, no per-test counts (no `-v`)
                                        //   no_tests     = no packages were detected
  packagesRun: 5, packagesFailed: 1,
  testsPass: 89, testsFail: 1, testsSkip: 0,
  failedTests: [{ package: "example/pkg/store", test: "TestEtag" }],
  nextCalls: [{                          // structured next-step actions
    tool: "runner_section",
    args: { runId, name: "example/pkg/store" },
    purpose: "Inspect failed package example/pkg/store ..."
  }]
}
delta: {
  newEvents: [...failed tests + assertion lines + section_starts/ends...],
  suppressedTestEvents: { passed: 89, skipped: 0, hint: "..." }
}
```

Passing/skipping per-test events are filtered from `delta.newEvents` (in
the run-level response) AND from `runner_section`'s events list (when
drilling into a package). Both surface a `suppressedTestEvents` field
counting what was hidden and a hint for `verbose: true`.

`runner_section` returns the section's failure events plus the captured
assertion lines, so you can see the exact failure without reading raw
logs. Pass `verbose: true` to also see passing tests.

**Adapter sections are atomic.** Unlike a manually-instrumented script,
the go-test adapter only emits a section once the package has finished
(it has to wait for the `ok pkg 1.234s` line). So during a multi-package
run, `activeSection` stays `null` and `recentlyChangedSections` fills in
as each package completes.

### Forcing or disabling adapters

- `parser: "auto"` (default) -- detect known formats; manual `::run::`
  markers always win.
- `parser: "none"` -- disable adapters; only marker mode.
- `parser: "go-test"` -- force the go-test adapter even when sniff
  doesn't match (e.g. output is interleaved with extra text).

When an adapter is in use, `runner_status` includes `parserUsed:
"<adapter-name>"` so you can trust the structure source.

---

## Writing instrumented scripts

You **never** write `::run::` lines yourself. Use a helper.

### Bash

```bash
#!/usr/bin/env bash
source "${XDG_DATA_HOME:-$HOME/.local/share}/runner-mcp/lib/runnerlog.sh"

runnerlog_section_start setup
mkdir -p /tmp/work
runnerlog_metric files=42
runnerlog_section_end setup ok exit=0

for T in a b c d e; do
    runnerlog_section_start "test-$T"
    if do_test "$T"; then
        runnerlog_section_end "test-$T" ok exit=0
    else
        rc=$?
        runnerlog_section_end "test-$T" failed exit=$rc reason="test-$T failed"
    fi
done
```

### Python (use the context manager)

```python
import sys, os
sys.path.insert(0, os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "runner-mcp", "lib"))
import runnerlog

with runnerlog.section("setup"):
    setup_things()
    runnerlog.metric(files=42)

for t in ["a", "b", "c"]:
    with runnerlog.section(f"test-{t}"):
        do_test(t)   # if this raises, section auto-closes with status=failed
```

### Anything else (Go, Make, awk, ...)

```bash
runnerlog section_start build
go build ./...
rc=$?
runnerlog section_end build ok exit=$rc        # or `failed exit=$rc reason="..."`
runnerlog event "compile complete" duration=12s
runnerlog metric files=42 bytes=12345
```

### Verb reference (instrumented scripts only)

| Verb | Use |
|------|-----|
| `section_start <name>` | Open a section. Anything emitted until the matching section_end is attributed to it. |
| `section_end <name> ok|failed [exit=N] [reason="..."]` | Close a section with its result. |
| `event "<msg>" [k=v ...]` | Discrete event inside the current section. |
| `metric k=v [k=v ...]` | Numeric/typed data attached to the current section. Merged across calls. |
| `fail "<msg>"` | Script-level fatal. The bash/python helpers also exit the script with code 1. |

You never write `::run::` lines yourself -- always go through a helper.

---

## Reading runner_status / blocking response

The response is decision-fields-first (look at the top, not the bottom).
Many fields are CONDITIONALLY emitted -- omitted when null/empty/zero --
so a clean success response stays small and a failure highlights only
the keys that matter:

```json
{
  // Always emitted
  "runId": "...",
  "name": "...",
  "pid": 1234,
  "startedAt": 1777246750,
  "state": "running",        // "starting" | "running" | "exited"
  "terminal": false,
  "result": null,            // null | "success" | "failed"
  "exitCode": null,
  "durationSec": 1,
  "lastEventAt": 1777246751,
  "sectionsDone": 0,
  "sectionsFailed": 0,
  "recentlyChangedSections": [ ... ],
  "delta": {
    "newEvents": [ ... ],
    "newEventCount": 7,
    "suppressedTestEvents": { "passed": 91, "skipped": 0, "hint": "..." },  // when adapter passing tests were filtered
    "truncatedEvents": 0     // when the cap was hit
  },
  "stderrCount": 0,

  // Conditionally emitted (omitted when not informative)
  "fatalMsg": "...",         // when runnerlog_fail was emitted
  "stalledForSec": 12,       // when > 0
  "activeSection": "test",   // when something is running (always null for adapter runs)
  "activeSectionState": {},  // compact snapshot of the running section
  "failedSections": [...],   // when nonempty
  "unknownSections": [...],  // sections that were open at exit (crashed mid-way)
  "stderrNewCount": 7,       // when > 0 and != stderrCount
  "stderrSample": [...],     // last N stderr lines, only on terminal failure
  "pollAfterSec": 15,        // recommended next poll cadence; omitted on terminal
  "parserUsed": "go-test",   // when an output adapter built the structure
  "restartCount": 1,         // when this service has been restarted
  "endpoints": ["http://localhost:5801/", "port:5800"],  // detected URLs/ports
  "stdoutTail": ["...last 15 stdout lines..."],  // terminal non-adapter runs only -- replaces `| tail -10`
  "warnings": {                                  // terminal runs only, when ERROR/FAIL/panic-shaped lines were found
    "count": 5,
    "sample": [{ "stream": "stdout", "lineNo": 42, "line": "ERROR: ..." }],
    "hint": "Output contains ERROR / FAIL / panic / fatal lines even though exit code may be 0..."
  },
  "runRoot": "/path/to/run/", // verbose:true only

  // testSummary: present when an adapter (e.g. go-test) ran.
  "testSummary": {
    "status": "failed",      // "failed" | "all_passed" | "packages_ok" | "no_tests"
    "packagesRun": 5,
    "packagesFailed": 1,
    "testsPass": 89,
    "testsFail": 1,
    "testsSkip": 0,
    "failedTests": [{ "package": "example/pkg/store", "test": "TestX" }],
    "nextCalls": [           // structured next-step actions; dispatch directly
      { "tool": "runner_section",
        "args": { "runId": "...", "name": "example/pkg/store" },
        "purpose": "Inspect failed package ..." }
    ]
  }
}
```

**Decision flow when reading this response:**
1. Check `result` -- success or failed?
2. If failed and `testSummary.nextCalls` is present, dispatch those calls
   directly. Each entry has `{ tool, args, purpose }` -- machine-readable
   so you don't have to parse English.
3. If failed without `testSummary`, look at `failedSections` and
   `unknownSections` and drill in with `runner_section`.
4. `delta.newEvents` is the "what's new since I last looked" stream --
   useful for tailing a long-running script. For adapter-driven runs,
   passing tests are suppressed; if you need them, pass `verbose: true`.
5. For services, `endpoints` tells you what URLs/ports to hit.
6. Many fields (fatalMsg, activeSection, failedSections, unknownSections,
   stalledForSec, stderrSample, stderrNewCount, runRoot) are
   conditionally emitted -- if you don't see them, there's nothing to
   report there. A clean success response is intentionally compact.

When the ~9 min wait elapses on a blocking-mode run (whether from the
initial `runner_start` or a follow-up `runner_status`), you get:

```json
{
  "stillRunning": true,
  "blockingWaitSec": 45,
  "followUp": "The 540s wait window elapsed; the run is STILL ACTIVE (NOT killed). Just call runner_status with runId='...' again -- it will automatically wait another 540s for this blocking run. Repeat until terminal:true. Use runner_kill to abort."
}
```

**This is the normal protocol** for any command longer than ~9 min, not
an error. Just call `runner_status` with the same runId. Repeat until
`terminal: true`.

---

## Searching log output

When the structured view isn't enough (rare), use grep:

```
runner_grep { runId, pattern: "panic:|FAIL|fatal" }
runner_grep { runId, pattern: "Test.*Login", stream: "stdout", A: 2, B: 1 }
runner_status { runId, grep: "deadlock", grepA: 3 }   // status + grep in one call
runner_section { runId, name: "example/pkg/store", grep: "etag" }
```

Returns matches as `[{ stream, lineNo, line, context: { before, after } }]`.

---

## Anti-patterns (DO NOT do these)

- **Don't call `runner_start` twice for the same service.** Use
  `runner_restart` to refresh, `runner_kill` to stop. Track the runId
  and a memorable `name`. If you've lost the runId, find it with
  `runner_list { state: "running", name: "<service-name>" }`.
- **Don't sleep + curl after starting a service.** Use `runner_wait_for`
  with the framework's ready signal; it returns the moment the
  service is up, with detected endpoints attached.
- **Don't pipe `head`/`tail`/`grep` into your cmd** to filter output.
  The runner gates these (refuses to run) and points you at its own
  grep. Run the producer command alone. If you really need a raw piped
  one-liner, set `noScrub: true` and own the consequences.
- **Don't shell out and grep for `go test` output.** The go-test
  adapter parses it for you. Call `runner_section { name: "<pkg>" }`
  for failures.
- **Don't poll faster than `pollAfterSec` recommends.** The runner
  suggests a cadence based on activity.
- **Don't ignore `unknownSections`.** Those are sections that crashed
  mid-way, distinct from `failedSections` (which closed cleanly with
  `failed` status).
- **Don't read raw stdout/stderr files** unless `runner_status`,
  `runner_section`, and `runner_grep` have all failed to give you what
  you need. The structured view is faster and more reliable.
- **Don't write `::run::` lines by hand.** Use a helper.
- **Don't pass `cwd` to follow-up tools out of habit.** The global runId
  index resolves any run by id alone. Only pass `cwd` to `runner_start`
  (where it sets the spawn directory).
- **Don't ignore `testSummary`.** When it's present (any go-test run),
  it gives you `packagesFailed`, `failedTests`, and a `followUp`
  pointing at the next call. That's your decision data.
- **Don't trust `result: "success"` blindly.** A non-empty `warnings`
  field means the output contained ERROR / FAIL / panic / fatal lines
  even though exit code was 0. Common with shell scripts and Makefiles
  that tolerate partial failures. When you see warnings, look at
  `stdoutTail` or call `runner_grep` to confirm what actually happened.
- **Don't reach for raw bash for non-trivial commands.** `runner_start`
  gives you stdoutTail, warnings, scrubbing, a queryable runId, and
  structured output for known formats. Bash gives you a wall of text.
- **Don't start a fresh sub-agent conversation when you could
  continue one.** `research_and_code_assistant_agent` without a
  `runId` spawns a brand-new conversation that has no memory of prior
  turns. If you're following up on the same investigation, pass the
  prior `runId` -- the sub-agent remembers and you save the token
  cost of re-establishing context. Use `runner_list` to find a prior
  conversation by name or by browsing `agent` blocks.
- **Don't delegate trivial work to a sub-agent.** Spinning up a peer
  agent costs real tokens and seconds. Use it for tasks where the
  delegation pays off: parallel investigations, second opinions on
  designs, self-contained subtasks. Don't use it to look up a single
  file or run one command -- do those yourself.
- **Don't poll `runner_status` to monitor an active sub-agent.**
  Each call dumps the full status response into your context. Use
  `logwatch` with `--duration N` for active tailing; reach for
  `runner_status` only when you need the structured snapshot. See
  "Dispatching sub-agents effectively" for the full playbook.
- **Don't kill a sub-agent for slow turns or high token counts.**
  Modern long-context models stay coherent past half a million
  tokens. Kill on provider errors, bad specs, or file-collision
  risk -- not impatience.
- **Don't trust a sub-agent's "all tests pass" claim.** Verify with
  `runner_start { cmd: "go test ./..." }` (or your project's test
  command) before merging. Sub-agents over-claim coverage.

---

## Storage and runId resolution

**Runs are scoped to YOUR project root**, not to where the cmd happens
to run. If your session cwd is inside a git repo, every run you start
lands at `<your-git-root>/.runner/<runId>/` -- even runs whose cmd
targets a totally different project. If your cwd is outside any git
repo, runs go to `~/.local/share/runner-mcp/<runId>/`.

Why this matters:
- `runner_list` only shows runs from YOUR project root. Another agent
  working in a different project sees only ITS runs. You don't get
  contaminated by their work.
- Two agents in the same project root DO see each other's runs.
  That's intentional -- if you're coordinated, you want the visibility.
- The `cwd` parameter on `runner_start` is the spawn working directory
  for the cmd (passed straight to the subprocess). It does NOT change
  where the run dir is stored.

**You do NOT need to pass `cwd` on follow-up calls.** The runner
maintains a global `~/.local/share/runner-mcp/index.jsonl` mapping every runId to its
storage path so `runner_status`, `runner_section`, `runner_grep`,
`runner_restart`, `runner_kill`, and `runner_wait_for` resolve a
runId without `cwd`.

Run dirs are auto-excluded from the host project's `git status` via
`.git/info/exclude` (local-only -- the project's tracked `.gitignore`
is never touched). The entry is added the first time the runner
spawns inside a given git repo.

Each run directory contains:

- `meta.json` -- cmd (exactly as given), parser, cwd,
  pid, start/end times, exit code, state, restartCount, previousEndedAt
- `stdout.log` -- raw stdout including any `::run::` markers
- `stderr.log` -- raw stderr
- `tracker.json` -- per-agent delta cursors

Events are parsed on demand from stdout.log (and synthesized by output
adapters when applicable). There is no separate events file.

`runner_purge` removes runs and returns a structured report. Modes:

- `{ runId: "..." }` -- remove that one run (cross-project via global index).
- `{ olderThan: 3600 }` -- remove terminal runs older than 1 hour.
- `{ result: "success" }` or `{ result: "failed" }` -- remove only terminal
  runs with that result.
- `{}` (no args) -- remove ALL terminal runs in your project root.

Active runs are NEVER purged. They come back in the `kept.active` list
so you see what's still going. Response shape:

```json
{
  "purged":  [{ "runId": "...", "name": "...", "result": "success", "durationSec": 12, "freedBytes": 5821 }],
  "kept": {
    "active":   [{ "runId": "...", "name": "fe-dev", "state": "running", "pid": 12345 }],
    "filtered": [{ "runId": "...", "name": "...", "result": "failed", "reason": "result='failed', filter wants 'success'" }]
  },
  "freedBytesTotal": 5821,
  "summary": { "purgedCount": 1, "keptActiveCount": 1, "keptFilteredCount": 1 },
  "scope": "/path/to/project/.runner"
}
```

---

## Quick recipes

```
# Run a generic build / install / migration:
runner_start { cmd: "make reinstall", cwd: "/path/to/project" }
# response.stdoutTail has the last 15 lines (no need for | tail -10)
# response.warnings flags ERROR / FAIL / panic / fatal lines even when exitCode == 0
# response.endpoints lists URLs/ports the script printed (useful for "I just brought up services")

# Run go tests, get failures with assertion lines:
runner_start { cmd: "go test ./...", cwd: "/path" }
# response includes failedSections + recentlyChangedSections with metrics
runner_section { runId, name: "<failed-package>" }
# response.events includes failure-detail lines

# Start a dev server and wait for it to be actually ready:
runner_start { cmd: "npm run dev", name: "fe", blocking: false, cwd: "/path/to/client" }
runner_wait_for { runId, pattern: "ready in" }
# response.endpoints lists the URLs ("http://localhost:5801/")
# If outcome="timeout" (~9 min elapsed), call runner_wait_for again to keep waiting.

# Start a Go server and wait until it's listening:
runner_start { cmd: "go run .", name: "api", blocking: false, cwd: "/path/to/server" }
runner_wait_for { runId, pattern: "listening on|server starting", stream: "stderr" }

# Refresh after a server code change (client keeps its HMR state):
runner_restart { runId }
runner_wait_for { runId, pattern: "listening on", stream: "stderr" }

# Stop a service:
runner_kill { runId }

# See all running services with their endpoints in one call:
runner_list { state: "running" }
# response: [{ name, endpoints: [...], lastLine, lastLineAgeSec, restartCount, ... }]

# Find a specific service if you've lost the runId:
runner_list { name: "hurricane", state: "running" }

# Long build (>9 min): keep calling runner_status until terminal:true.
runner_start { cmd: "make all" }
# -> stillRunning:true (build is still running). Follow up:
runner_status { runId }
# -> auto-waits another ~9 min, may return stillRunning:true again. Just repeat:
runner_status { runId }
# ... until terminal:true. Then read result, exitCode, stdoutTail, warnings.

# Delegate a code review to a peer agent (new conversation):
research_and_code_assistant_agent {
  ask: "Read src/auth and identify any token validation paths that don't check expiry."
}
# -> { runId, agent: { runtime: "opencode", turn: 1, toolCallCount: 4 }, stdout: [...] }

# Continue the same investigation across turns:
research_and_code_assistant_agent {
  runId,                                             # same runId = same conversation
  ask: "Good. Now check if any of those paths are reachable from unauthenticated endpoints."
}
# -> { runId, agent: { turn: 2 }, stdout: [...follow-up findings...] }

# Find conversations you started earlier:
runner_list { name: "opencode-agent" }
# -> entries with `agent: { runtime, turn, backendSessionId }`; pick a runId to continue.
```
