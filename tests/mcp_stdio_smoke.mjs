// End-to-end smoke test of an installed runner MCP server over stdio, the way
// an agent drives it. Platform-neutral; written for the Windows port.
//
//   node tests/mcp_stdio_smoke.mjs <server index.js> <project dir> [phase]
//
// phase "all" (default): initialize, tools/list, runner_guide, a blocking
// run, the chain gate, a service with wait_for / restart / kill.
// phase "detach": start a service with blocking:false, print its runId and
// exit. libuv puts this script's children (the MCP server, and through it the
// core) in a kill-on-close Job Object on Windows, so the service is still
// running afterwards only if runner detached it. Check with the "check" phase.
// phase "check <runId>": runner_status + kill for that run.
import { spawn } from "child_process";

const [serverPath, projectDir, phase = "all", runIdArg] = process.argv.slice(2);
const server = spawn(process.execPath, [serverPath], { cwd: projectDir, stdio: ["pipe", "pipe", "inherit"] });

let buf = "";
const waiting = new Map();
server.stdout.on("data", (d) => {
  buf += d.toString();
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    const msg = JSON.parse(line);
    if (msg.id != null && waiting.has(msg.id)) { waiting.get(msg.id)(msg); waiting.delete(msg.id); }
  }
});
let nextId = 1;
function rpc(method, params) {
  const id = nextId++;
  server.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
  return new Promise((res) => waiting.set(id, res));
}
async function call(name, args) {
  const r = await rpc("tools/call", { name, arguments: args });
  if (r.error) throw new Error(`${name}: ${JSON.stringify(r.error)}`);
  // The first block is the payload; some tools append a reminder block.
  const text = r.result.content[0].text;
  try { return JSON.parse(text); } catch { return text; }
}
let failures = 0;
function check(label, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"} ${label}${detail ? " -- " + detail : ""}`);
  if (!ok) failures++;
}

await rpc("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "smoke", version: "0" } });
server.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");

if (phase === "detach") {
  const s = await call("runner_start", { cmd: "node srv.js", name: "detach-probe", blocking: false });
  console.log(`RUNID ${s.runId}`);
  process.exit(0); // closes our job: kills the MCP server, and anything not detached
}

if (phase === "check") {
  const st = await call("runner_status", { runId: runIdArg, wait: false });
  check("detached service still running after its MCP client exited", st.state === "running", `state=${st.state}`);
  const k = await call("runner_kill", { runId: runIdArg });
  check("kill the detached service", k.killed === true);
  server.kill();
  process.exit(failures ? 1 : 0);
}

const tools = (await rpc("tools/list", {})).result.tools.map((t) => t.name);
check("tools/list", tools.includes("runner_start") && tools.includes("runner_guide"), tools.join(","));

const guide = await call("runner_guide", {});
check("runner_guide includes the Windows section", typeof guide === "string" && guide.includes("# Runner on Windows"));

const one = await call("runner_start", { cmd: 'echo "hello from $(uname -s)"; echo oops >&2; exit 2', noScrub: true });
check("blocking run: exit code", one.exitCode === 2, `exitCode=${one.exitCode}`);
check("blocking run: stdout captured", JSON.stringify(one.stdout || one.stdoutTail || "").includes("hello from MINGW") || JSON.stringify(one).includes("hello from"), JSON.stringify(one.stdout));
check("blocking run: stderr separate", JSON.stringify(one.stderr || "").includes("oops"));

const gated = await call("runner_start", { cmd: "echo a && echo b" });
check("chain is gated, not run", JSON.stringify(gated).includes("&&") && !gated.runId, JSON.stringify(gated).slice(0, 120));

const ps = await call("runner_start", { cmd: "powershell -NoProfile -Command 'Write-Output PS$($PSVersionTable.PSVersion.Major)'" });
check("PowerShell as a single command", /PS[57]/.test(JSON.stringify(ps.stdout)), JSON.stringify(ps.stdout));

const svc = await call("runner_start", { cmd: "node srv.js", name: "svc", blocking: false });
const w = await call("runner_wait_for", { runId: svc.runId, pattern: "running on port" });
check("service: wait_for matched", w.outcome === "matched", `outcome=${w.outcome}`);
const st = await call("runner_status", { runId: svc.runId, wait: false });
check("service: running with endpoint", st.state === "running" && JSON.stringify(st.endpoints || []).includes("5899"), JSON.stringify(st.endpoints));
const rs = await call("runner_restart", { runId: svc.runId });
check("service: restart under the same runId", rs.runId === svc.runId && rs.restartCount === 1);
const w2 = await call("runner_wait_for", { runId: svc.runId, pattern: "running on port" });
check("service: up again after restart", w2.outcome === "matched");
const ls = await call("runner_list", { state: "running", name: "svc" });
check("runner_list finds it", JSON.stringify(ls).includes(svc.runId));
const k = await call("runner_kill", { runId: svc.runId });
check("service: kill", k.killed === true);
const st2 = await call("runner_status", { runId: svc.runId, wait: false });
check("service: exited after kill", st2.state === "exited", `state=${st2.state}`);

server.kill();
console.log(failures ? `${failures} FAILED` : "ALL PASSED");
process.exit(failures ? 1 : 0);
