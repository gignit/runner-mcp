package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
)

// dataHome is the root the installer puts runner-mcp under: $XDG_DATA_HOME
// when set, else ~/.local/share on every platform (on Windows too: the
// Microsoft Store Python virtualizes %LOCALAPPDATA%). runner_core.py and the
// MCP server resolve the same path.
func dataHome(home string) string {
	if x := os.Getenv("XDG_DATA_HOME"); x != "" {
		return x
	}
	if home == "" {
		return ""
	}
	return filepath.Join(home, ".local", "share")
}

var (
	pythonOnce sync.Once
	pythonArgv []string
)

var python3Version = regexp.MustCompile(`Python 3\.`)

// pythonCommand returns the argv prefix that runs Python 3 for the core.
// `python3` on macOS/Linux; on Windows (where python3 is often absent or the
// Microsoft Store stub): $RUNNER_PYTHON, then the interpreter install.ps1
// recorded in <install>/python.path, then python3 / python / py -3,
// whichever really is Python 3. Same order as the MCP server.
func pythonCommand(corePath string) []string {
	pythonOnce.Do(func() {
		if env := strings.TrimSpace(os.Getenv("RUNNER_PYTHON")); env != "" {
			pythonArgv = []string{env}
			return
		}
		recorded := filepath.Join(filepath.Dir(filepath.Dir(corePath)), "python.path")
		if b, err := os.ReadFile(recorded); err == nil {
			if p := strings.TrimSpace(string(b)); p != "" && fileExists(p) {
				pythonArgv = []string{p}
				return
			}
		}
		if runtime.GOOS != "windows" {
			pythonArgv = []string{"python3"}
			return
		}
		for _, cand := range [][]string{{"python3"}, {"python"}, {"py", "-3"}} {
			cmd := exec.Command(cand[0], append(cand[1:], "--version")...)
			hideWindow(cmd)
			out, err := cmd.CombinedOutput()
			if err == nil && python3Version.Match(out) {
				pythonArgv = cand
				return
			}
		}
		pythonArgv = []string{"python"}
	})
	return pythonArgv
}

// coreCommand builds the exec.Cmd that runs `runner_core.py <args>`.
func coreCommand(corePath string, args ...string) *exec.Cmd {
	py := pythonCommand(corePath)
	argv := append(append(append([]string{}, py[1:]...), corePath), args...)
	cmd := exec.Command(py[0], argv...)
	hideWindow(cmd)
	return cmd
}
