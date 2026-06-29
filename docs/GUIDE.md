# Runner -- Agent Guide

The tool descriptions are the primary reference. This guide adds the
purpose and the facts they don't cover. Direct, no padding.

## runner_status is the only source of truth (aborts are not)

Runner detaches every process. A wait NEVER kills it. So these are all
NON-authoritative about a run and must be VERIFIED, never assumed:

- an `aborted` / `interrupted` notice on a `runner_wait_for` (or a
  blocking `runner_start`) -- that is the TOOL CALL ending, not the
  run. The process keeps going, detached, to completion.
- a `runner_wait_for` `timeout` -- the pattern wasn't seen yet; the run
  is still alive.
- a `matched` -- only that the LINE appeared. An early banner
  (`starting up`) matches a loose pattern while work continues; a match
  says nothing about the eventual exit code.

After ANY of these, call `runner_status` (wait=false) for the real
`state` / `result` / `exitCode`. It may be running, have finished
successfully, or have failed -- three different realities behind the
same "aborted". Treating an abort as a failure (or an early match as
"ready/done") is a real, cycle-costing mistake. When waiting for
readiness, also tighten the pattern to the TRUE ready line, not the
first banner.

## What runner is for

- **Background process management.** Start dev servers (vite, APIs),
  builds, tests, and long-running scripts; restart, kill, list, and
  poll them by runId without orphaning processes or re-running work
  just to see output again.
- **Token-efficient, standardized output.** Every run's FULL
  stdout/stderr is captured to disk and served back decision-first:
  `state`, `result`, `exitCode`, `endpoints`, `testSummary`,
  `warnings`, plus a delta of only what's new since you last looked.
  You never trade completeness for context budget.
- **Multi-agent collaboration.** Delegate to opencode, Claude Code, or
  Codex peer agents with true multi-turn conversations: session resume
  across turns, and adoption of sessions started outside runner
  (opencode `ses_...`; Claude/Codex UUIDs, including cross-project
  sessions). Choose with `agent`; opencode remains the default.
- **Remote / SSH commands.** Run `ssh host '...'` builds, installs, and
  deploys through runner: the full remote stdout/stderr is captured, the
  run survives your own disconnect, and you poll it by runId like any
  other. The command gate is quote-aware, so a `&&`/`|` INSIDE the
  remote command (e.g. `ssh host 'cd repo && make install'`) runs
  verbatim and is not gated -- only unquoted, local chain operators are.

## Replace filter pipes -- don't wrap commands in them

Never shrink output with `head | tail | grep | sed | awk` or `echo`
decoration. Those pipes swallow the error lines you actually need and
mask exit codes. Run the bare command via `runner_start`: the full
output is stored, error conditions surface automatically (`warnings`,
`failedSections`, `stderrSample`, `testSummary`), and you filter
AFTER capture -- repeatably -- with `runner_grep`, `runner_section`,
or `runner_status`'s embedded `grep`. This is why `runner_start`
gates filter pipes and `&&/||/;` chains.

## Multi-step chains (&&, ||, ;) -- two ways out

A `&&`/`||`/`;` chain is gated. The rejection NAMES the offending
operator and hands you two fixes; pick one, don't re-derive it:

- **Split it.** Run each step as its own `runner_start`, in order.
  The gate response includes the exact per-step commands ready to
  dispatch. Best for a few steps. Caveat: separate runs do NOT share
  shell state -- a `cd` or env var from step 1 is gone by step 2, so
  fold it into that step's own `cmd` (or use `cwd=`).
- **Script it.** Put the steps in a script (below). Best for
  repeated or complex work.

The only reason to keep a chain as one command is genuine shared
shell state that neither fix covers (e.g. `cd x && ./run`); then
re-send with `noScrub: true`.

## Reusable scripts over bash one-liners

Multi-step work belongs in a script (`scripts/` in the project, or
`.runner/scripts/`) taking CLI parameters -- not a quoted `bash -c`
chain that needs escape gymnastics. Scripts are rerunnable,
reviewable, and diffable. Optionally source the runner-supplied
libraries (call `runner_helpers` for paths and paste-ready snippets)
to emit sections/metrics/events that `runner_section` reads as
structure. Never hand-write `::run::` protocol lines.

## Facts the tool descriptions don't cover

**Run dir layout.** `<git-root>/.runner/<runId>/` holds `stdout.log`,
`stderr.log`, `meta.json`. For sub-agent runs, `stdout.log` is the
conversation text only; `stderr.log` is the raw backend event stream
(NDJSON); prior prompts archive to `prompts/<N>.md`. `runner_grep`
scopes to the current turn unless `allTurns: true`.

**Test runs: the normalized testSummary.**

`go test` and `pytest` are auto-detected from their output -- no
instrumentation, no flag. When detected, `parserUsed` is set
(`"go-test"` / `"pytest"`) and `testSummary` is the AUTHORITATIVE view.
Read it first; it is framework-agnostic on purpose, so the same keys
mean the same thing whichever runner produced them:

- `testSummary.framework` -- which adapter ran.
- `testSummary.status` -- `passed` (per-test data, all green),
  `failed`, `packages_ok` (packages green but no per-test breakdown,
  e.g. `go test ./...` without `-v` -- this IS success, not "no tests
  ran"), or `no_tests`.
- `testSummary.packages` -- `{run, passed, failed}`. For pytest each
  test FILE counts as a package.
- `testSummary.tests` -- `{passed, failed, skipped}` when per-test
  counts exist, else `perTestCountsAvailable:false` + a hint (trust
  `packages` in that case).
- `testSummary.failures[]` -- one entry per failing test, each with
  the assertion `detail` inlined (testify blocks, plain `t.Error`,
  and pytest reasons are captured + cleaned), plus a `logRef
  {section, fromLine, toLine}` you can hand to `runner_section`. On a
  common handful of failures you can usually fix the bug from this
  alone -- no drill-down round-trip.
- `testSummary.nextCalls` -- ready-to-dispatch `{tool, args, purpose}`
  (one `runner_section` per failed package). Call them directly
  instead of parsing prose.
- `testSummary.slowestPackages` -- top few by duration, green runs
  only.

The response is proportional to FAILURES, not suite size: a green
93-package run collapses to a compact summary; a red run inlines
exactly the failing tests. The full per-package/per-test stream always
stays on disk -- reach it with `runner_section`, `runner_grep`, or
`verbose:true`. If an adapter run FAILED, the redundant `warnings`
block is intentionally omitted (the failures are already in
`testSummary.failures[]`); `warnings` still appears for a GREEN run
with error-shaped output ("don't trust a green result").

**Other response fields.**

- `delta.suppressedTestEvents` = passing/skipped per-test events
  filtered out; `delta.suppressedSectionEvents` = passing-package
  section/metric events; `delta.suppressedDetailEvents` = raw
  assertion-detail lines already inlined in `testSummary.failures[]`.
  Each is a count with a hint; `verbose:true` (or `runner_section`)
  restores the raw stream. For an adapter run, `delta.newEvents`
  keeps only failing-test markers and failed-section boundaries.
- `delta` cursors are per-caller (the `agent` param on
  `runner_status`), so two agents can poll one run without stealing
  each other's "new output".
- `unknownSections` = sections still open at process exit -- the run
  crashed mid-way through them.

**Getting the adapter to engage.** Detection sniffs the first ~60
lines. `go test` is recognized immediately (`=== RUN` / `ok pkg`).
pytest is recognized by its session banner, `-v` per-test lines, or
the `N passed in Xs` result line. A long `-q` pytest run whose only
signal is a result line far past the sniff window may not
auto-detect -- pass `parser: "pytest"` to `runner_start` to force it.
To fix a failure, read `testSummary.failures[].detail` first; fall
back to `runner_section` (via the supplied `nextCalls`) only when you
need the full traceback.

**Sub-agent response shape.** A `research_and_code_assistant_agent`
call returns the peer agent's answer decision-first, not a transcript:

- `finalReply.text` -- the sub-agent's COMPLETE reply for the turn.
  This is the answer; read it, don't go hunting in the logs.
- `agent` block -- `runtime` (opencode/claude/codex), `turn` count,
  `backendSessionId`, `toolCallCount`, `tokensSoFar`, `lastReason`
  (`completed` / `interrupted`). Confirms which backend ran and how
  much work it did.
- `transcriptHint` -- points at `stdout.log` (rendered transcript) for
  the full turn-by-turn detail; drill in with `runner_grep` /
  `runner_section` only when `finalReply` isn't enough.
- Still running? A blocking call that hits the wait window (or any
  `blocking:false` dispatch) returns `stillRunning` + a follow-up hint;
  poll with `runId` alone (no `ask`) to collect `finalReply` when
  ready. `runner_list` flags a wedged turn as `stalled:true` + `idleSec`.

**Sub-agent turn recovery.**

- `interrupted` in a response means the backend's LLM call failed
  (rate limit, API error). The session survives -- send a new `ask`
  on the same runId to resume.
- A new `ask` on a runId auto-kills any in-flight turn first; that is
  the idiom for "stop what you're doing, do this instead".
  `runner_kill` also preserves the conversation (its response carries
  a resumeHint).
- Token total dropping sharply between turns while cache reads spike
  means the backend compacted its context: intent survives,
  mechanical specifics don't. Restate file:line locations and exact
  symbol names in the next ask.

**Parallel dispatch.** `blocking: true` calls serialize on the MCP
transport. To actually run sub-agents in parallel, dispatch each with
`blocking: false` and poll the runIds afterward. Runner does not
protect two sub-agents editing the same files -- partition the work.
