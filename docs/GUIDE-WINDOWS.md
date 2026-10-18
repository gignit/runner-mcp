# Runner on Windows

Runner on Windows works the way it does on macOS and Linux: same tools, same
responses, same rules. This section covers what is different.

## What runs your command

`cmd` runs under **Git Bash** (Git for Windows' `bash.exe`, never WSL's
`System32\bash.exe`). So bash syntax, the pipe and chain gating, and
`.runner/scripts/*.sh` all work exactly as the rest of this guide says.
Every response's run meta records the shell as `shell` / `shellPath`.

- PowerShell scripts and tools: run them as one command, e.g.
  `powershell -NoProfile -ExecutionPolicy Bypass -File Tools/build.ps1 -Target x`.
  Use `pwsh` for PowerShell 7.
- cmd.exe built-ins: `cmd //c "dir /b"` (Git Bash needs `//c`, not `/c`).
- Paths: forward slashes work everywhere (`D:/src/app`, `C:/Users/me`).
  Quote paths with spaces. `/c/Users/...` is Git Bash-only; the `cwd`
  parameter takes a normal Windows path.
- Another default shell for every run: set `RUNNER_SHELL` to `pwsh`,
  `powershell` or `cmd` in the MCP server's environment (the gating still
  reads the command as bash).

## The habit to drop: one long chained line

Without runner, agents on Windows tend to send one long line like this:

```
cd /c/proj; sed -n 8,24p docs/X.md; ls Tools | grep -i perf; start=$(date +%s);
powershell -File Tools/build.ps1 2>&1 | grep result | cut -c1-300;
echo "took $(( $(date +%s) - start )) s"; head -15 Temp/out.txt
```

Every part of that line is something runner (or your own file tools) does
better:

| In the one-liner | With runner |
|---|---|
| `cd /c/proj;` | `cwd: "C:/proj"` on `runner_start` |
| `sed -n` / `head` / `ls \| grep` to read files | your file-read and search tools, not a shell |
| `start=$(date)` ... `echo took N s` | `durationSec` is in every terminal response |
| `2>&1 \| grep result \| cut` | run it bare: `runner_start { cmd: "powershell -NoProfile -ExecutionPolicy Bypass -File Tools/build.ps1" }`, then `runner_grep { runId, pattern: "result" }` |
| the build being killed when the tool call times out | it is detached, so it never is: poll `runner_status` |

## Detached runs and kill

Every run is started by a small supervisor process that leaves the agent's
Job Object, so the run survives when the agent is stopped, the session
ends, or the MCP server restarts. The run's meta records how it got out as
`detach`:

- `breakaway` (normal) or `free`: fully detached.
- `wmi`: the agent's job forbade breakaway, so runner created the
  supervisor through WMI instead. Still fully detached.
- `job-bound`: no way out worked. The run behaves normally but ends with
  the agent's session; restart it from a terminal if it has to outlive
  the session.

`runner_kill` and `runner_restart` end the whole process tree (the shell,
`npm`, the `node`/`vite` it started, anything else it spawned), using the
run's own Job Object. Nothing is left holding the port.

## Output capture

stdout and stderr are captured separately through pipes (Windows has no
pty that keeps the two apart). Python (runner sets `PYTHONUNBUFFERED=1`),
Node, Go, .NET and PowerShell write each line as it happens. Some native
C/C++ tools buffer when writing to a pipe, so their output can arrive in
bursts or only when they exit. That is normal and the run is fine: check
`runner_status` rather than assuming a quiet run is stuck. `COLUMNS=200`
is set so width-aware tools don't wrap at 80.

## Instrumented scripts

- bash: `source` the `runnerlog.sh` path from `runner_helpers`. It works
  unchanged under Git Bash.
- PowerShell, or cmd: call the `runnerlog` CLI (`runnerlog.cmd` is on
  PATH after install), e.g. `runnerlog section_start build`, then
  `runnerlog section_end build ok exit=$LASTEXITCODE`.
- Python: `import runnerlog`, as in the `runner_helpers` snippet.
  `RUNNER_PYTHON` (set in every run) is the interpreter runner itself
  uses. Use it instead of `python3`, which on Windows is often the
  Microsoft Store stub.

## Where things live

- Install: `%LOCALAPPDATA%\runner-mcp` (`runner-cli.exe` and `runnerlog.cmd`
  in its `bin\`, which is on the user PATH).
- Run data: `<git-root>\.runner\<runId>\`, same as everywhere. Runs
  started outside a git repo go under the install dir.
