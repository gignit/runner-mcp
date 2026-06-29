package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/atotto/clipboard"
	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
)

type runnerClient struct {
	corePath string
}

type runSummary struct {
	RunID          string   `json:"runId"`
	Name           string   `json:"name"`
	State          string   `json:"state"`
	Result         string   `json:"result"`
	DurationSec    int      `json:"durationSec"`
	StartedAt      int64    `json:"startedAt"`
	ExitCode       int      `json:"exitCode"`
	PID            int      `json:"pid"`
	RunRoot        string   `json:"runRoot"`
	Description    string   `json:"description"`
	LastLine       string   `json:"lastLine"`
	LastLineAgeSec int      `json:"lastLineAgeSec"`
	StderrCount    int      `json:"stderrCount"`
	Endpoints      []string `json:"endpoints"`
}

// runMeta is the subset of a run's meta.json the TUI cares about for the
// Input panel. The full file (written by core/runner_core.py) has many more
// fields; encoding/json ignores the ones we don't declare.
type runMeta struct {
	Cmd          string `json:"cmd"`
	AgentRuntime string `json:"agentRuntime"`
}

type appAction struct {
	Name           string
	Command        string
	PromptLabel    string
	Placeholder    string
	Description    string
	Warning        string
	NeedsPrompt    bool
	NeedsSelection bool
	Fields         []fieldSpec
}

type fieldKind int

const (
	fieldText fieldKind = iota
	fieldEnum
)

type fieldSpec struct {
	Name        string
	Label       string
	Kind        fieldKind
	Required    bool
	Placeholder string
	Default     string
	Options     []string
}

type commandField struct {
	spec      fieldSpec
	input     textinput.Model
	enumIndex int
}

type rect struct {
	x int
	y int
	w int
	h int
}

func (r rect) contains(x, y int) bool {
	return x >= r.x && x < r.x+r.w && y >= r.y && y < r.y+r.h
}

type listMsg struct {
	jobs []runSummary
	err  error
}

type logsMsg struct {
	runID  string
	stdout string
	stderr string
	err    error
}

type actionMsg struct {
	name       string
	raw        string
	runID      string
	statusMsg  string
	filter     *grepFilter
	filterName string
	err        error
}

type grepFilter struct {
	pattern       string
	stdout        string
	stderr        string
	totalMatches  int
	stdoutMatches int
	stderrMatches int
	truncated     bool
}

type grepResponse struct {
	Pattern      string      `json:"pattern"`
	Matches      []grepMatch `json:"matches"`
	TotalMatches int         `json:"totalMatches"`
	Truncated    bool        `json:"truncated"`
}

type grepMatch struct {
	Stream  string `json:"stream"`
	LineNo  int    `json:"lineNo"`
	Line    string `json:"line"`
	Context struct {
		Before []string `json:"before"`
		After  []string `json:"after"`
	} `json:"context"`
}

type tickMsg time.Time

type paneFocus int

const (
	focusJobs paneFocus = iota
	focusInput
	focusStdout
	focusStderr
	focusPrompt
	focusModal
)

// headerButton is a clickable panel-toggle target in the header button bar.
type headerButton struct {
	name string
	rect rect
}

type model struct {
	runner   runnerClient
	scopeDir string

	width  int
	height int

	jobs        []runSummary
	selectedIdx int
	selectedID  string
	autoMode    bool
	knownRunIDs map[string]struct{}
	runIDsReady bool

	stdoutContent string
	stderrContent string
	inputContent  string
	fullStdout    string
	fullStderr    string
	filterActive  bool
	filterTitle   string
	inputVisible  bool
	stdoutVisible bool
	stderrVisible bool
	outputSplit   float64
	stdoutFollow  bool
	stderrFollow  bool

	jobsVP   viewport.Model
	inputVP  viewport.Model
	stdoutVP viewport.Model
	stderrVP viewport.Model
	modalVP  viewport.Model

	prompt      textinput.Model
	actions     []appAction
	actionIdx   int
	commandMode bool
	fields      []commandField
	fieldIdx    int
	focus       paneFocus
	busy        bool
	busyAction  string

	statusText string
	statusErr  bool
	modalOpen  bool
	modalTitle string

	jobsRect    rect
	inputRect   rect
	stdoutRect  rect
	stderrRect  rect
	promptRect  rect
	dividerRect rect
	actionRects []rect

	// headerButtons are the clickable panel-toggle targets rendered in the
	// header button bar. inputCloseRect/stdoutCloseRect/stderrCloseRect are
	// the [x] close buttons inside each panel title. All are recomputed on
	// every render so mouse hit-testing stays in sync with what's drawn.
	headerButtons   []headerButton
	inputCloseRect  rect
	stdoutCloseRect rect
	stderrCloseRect rect

	draggingDivider  bool
	jobsPinnedBottom bool

	// Drag-to-select-and-copy state. While selecting is true, the user is
	// dragging across selectPane; anchor is the press point and end tracks the
	// current cursor. Line/col are indices into the pane's wrapped viewport
	// content (the same lines the viewport splits on).
	selecting     bool
	selectPane    paneFocus
	selAnchorLine int
	selAnchorCol  int
	selEndLine    int
	selEndCol     int
}

func main() {
	scopeDir, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "runner-mcp: failed to get cwd: %v\n", err)
		os.Exit(1)
	}

	corePath, err := findCorePath()
	if err != nil {
		fmt.Fprintf(os.Stderr, "runner-mcp: %v\n", err)
		os.Exit(1)
	}

	m := newModel(scopeDir, corePath)
	p := tea.NewProgram(m, tea.WithAltScreen(), tea.WithMouseCellMotion())
	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "runner-mcp: %v\n", err)
		os.Exit(1)
	}
}

func newModel(scopeDir, corePath string) model {
	prompt := textinput.New()
	prompt.Prompt = "› "
	prompt.CharLimit = 0
	prompt.Focus()

	jobsVP := viewport.New(0, 0)
	inputVP := viewport.New(0, 0)
	stdoutVP := viewport.New(0, 0)
	stderrVP := viewport.New(0, 0)
	modalVP := viewport.New(0, 0)

	m := model{
		runner: runnerClient{
			corePath: corePath,
		},
		scopeDir:         scopeDir,
		inputVisible:     false,
		stdoutVisible:    true,
		stderrVisible:    true,
		outputSplit:      0.58,
		stdoutFollow:     true,
		stderrFollow:     true,
		jobsVP:           jobsVP,
		inputVP:          inputVP,
		stdoutVP:         stdoutVP,
		stderrVP:         stderrVP,
		modalVP:          modalVP,
		prompt:           prompt,
		actions:          buildActions(),
		focus:            focusPrompt,
		statusText:       "Loading runner jobs…",
		selectedIdx:      -1,
		autoMode:         true,
		knownRunIDs:      make(map[string]struct{}),
		jobsPinnedBottom: true,
	}
	m.syncPromptMeta()
	return m
}

func buildActions() []appAction {
	return []appAction{
		{
			Name:           "grep",
			Command:        "grep",
			PromptLabel:    "Regex",
			Placeholder:    "panic|FAIL|ready",
			Description:    "Search the selected run's stdout and stderr with runner_grep.",
			NeedsPrompt:    true,
			NeedsSelection: true,
			Fields: []fieldSpec{
				textField("pattern", "Pattern", true, "panic|FAIL|ready"),
				enumField("stream", "Stream", false, "both", "both", "stdout", "stderr"),
				textDefaultField("before", "Before", false, "0", "lines before each match"),
				textDefaultField("after", "After", false, "0", "lines after each match"),
				textDefaultField("limit", "Limit", false, "200", "max matches"),
				enumField("ignoreCase", "Ignore Case", false, "false", "false", "true"),
			},
		},
		{
			Name:           "status",
			Command:        "status",
			PromptLabel:    "Optional grep",
			Placeholder:    "leave blank for a plain status snapshot",
			Description:    "Fetch a structured runner_status snapshot. Optional prompt becomes an embedded grep.",
			NeedsSelection: true,
			Fields: []fieldSpec{
				textField("grep", "Grep", false, "optional regex"),
				enumField("grepStream", "Grep Stream", false, "both", "both", "stdout", "stderr"),
				enumField("wait", "Wait", false, "false", "false", "true"),
			},
		},
		{
			Name:           "wait-for",
			Command:        "wait-for",
			PromptLabel:    "Ready regex",
			Placeholder:    "listening on|ready in|startup complete",
			Description:    "Block until the selected run emits a matching log line.",
			NeedsPrompt:    true,
			NeedsSelection: true,
			Fields: []fieldSpec{
				textField("pattern", "Pattern", true, "listening on|ready in|startup complete"),
				enumField("stream", "Stream", false, "both", "both", "stdout", "stderr"),
				enumField("ignoreCase", "Ignore Case", false, "false", "false", "true"),
			},
		},
		{
			Name:        "start",
			Command:     "start",
			PromptLabel: "Command",
			Placeholder: "go test ./... or npm run dev",
			Description: "Spawn a new runner job in this TUI's current working directory. It starts non-blocking so you can watch it live.",
			Warning:     "⚠ Starting a job changes runner state and may alter what another agent sees in the project.",
			NeedsPrompt: true,
			Fields: []fieldSpec{
				textField("cmd", "Command", true, "go test ./... or npm run dev"),
				textField("name", "Name", false, "optional run name"),
				textField("description", "Description", false, "optional description"),
				enumField("blocking", "Blocking", false, "false", "false", "true"),
			},
		},
		{
			Name:           "restart",
			Command:        "restart",
			PromptLabel:    "No prompt needed",
			Placeholder:    "press Enter to restart the selected run",
			Description:    "Kill and respawn the selected run under the same runId.",
			Warning:        "⚠ Restart mutates the selected run and can invalidate another agent's assumptions about its process state.",
			NeedsSelection: true,
			Fields:         []fieldSpec{},
		},
		{
			Name:           "kill",
			Command:        "kill",
			PromptLabel:    "No prompt needed",
			Placeholder:    "press Enter to kill the selected run",
			Description:    "Send SIGKILL to the selected run's process group.",
			Warning:        "⚠ Kill interrupts the selected run immediately and can confuse an agent expecting it to keep running.",
			NeedsSelection: true,
			Fields:         []fieldSpec{},
		},
		{
			Name:        "purge",
			Command:     "purge",
			PromptLabel: "Filter",
			Placeholder: "success, failed, olderThan=3600, runId=<id>, or blank",
			Description: "Remove terminal run directories in this project scope, optionally filtered by result, age, or an explicit runId.",
			Warning:     "⚠ Purge deletes run history and log scrollback from disk. Blank prompt purges all terminal runs in scope.",
			Fields: []fieldSpec{
				enumField("mode", "Mode", false, "terminal", "terminal", "success", "failed", "older-than", "run-id"),
				textField("value", "Value", false, "seconds for older-than, runId for run-id"),
			},
		},
	}
}

func textField(name, label string, required bool, placeholder string) fieldSpec {
	return fieldSpec{Name: name, Label: label, Kind: fieldText, Required: required, Placeholder: placeholder}
}

func textDefaultField(name, label string, required bool, defaultValue, placeholder string) fieldSpec {
	return fieldSpec{Name: name, Label: label, Kind: fieldText, Required: required, Default: defaultValue, Placeholder: placeholder}
}

func enumField(name, label string, required bool, defaultValue string, options ...string) fieldSpec {
	return fieldSpec{Name: name, Label: label, Kind: fieldEnum, Required: required, Default: defaultValue, Options: options}
}

func (m model) Init() tea.Cmd {
	return tea.Batch(loadJobsCmd(m.runner, m.scopeDir), tickCmd())
}

func tickCmd() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg {
		return tickMsg(t)
	})
}

func loadJobsCmd(r runnerClient, scopeDir string) tea.Cmd {
	return func() tea.Msg {
		jobs, err := r.list(scopeDir)
		return listMsg{jobs: jobs, err: err}
	}
}

func loadLogsCmd(runID, runRoot string) tea.Cmd {
	return func() tea.Msg {
		stdoutPath := filepath.Join(runRoot, "stdout.log")
		stderrPath := filepath.Join(runRoot, "stderr.log")

		stdout, err := os.ReadFile(stdoutPath)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return logsMsg{runID: runID, err: err}
		}
		stderr, err := os.ReadFile(stderrPath)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return logsMsg{runID: runID, err: err}
		}

		return logsMsg{
			runID:  runID,
			stdout: string(stdout),
			stderr: string(stderr),
		}
	}
}

func actionCmd(r runnerClient, scopeDir string, action appAction, selected *runSummary, values map[string]string) tea.Cmd {
	return func() tea.Msg {
		raw, runID, statusMsg, filter, err := r.runAction(scopeDir, action, selected, values)
		return actionMsg{
			name:       action.Name,
			raw:        raw,
			runID:      runID,
			statusMsg:  statusMsg,
			filter:     filter,
			filterName: action.Name,
			err:        err,
		}
	}
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmds []tea.Cmd

	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.layout()
		m.refreshViewports()
	case tickMsg:
		cmds = append(cmds, tickCmd(), loadJobsCmd(m.runner, m.scopeDir))
		if job := m.selectedJob(); job != nil {
			if m.inputVisible {
				m.loadInputContent()
				m.refreshLogViewports()
			}
			cmds = append(cmds, loadLogsCmd(job.RunID, job.RunRoot))
		}
	case listMsg:
		if msg.err != nil {
			m.statusErr = true
			m.statusText = msg.err.Error()
			break
		}
		if m.statusText == "Loading runner jobs…" {
			m.statusText = fmt.Sprintf("Loaded %d runner jobs.", len(msg.jobs))
			m.statusErr = false
		}
		m.jobs = sortJobs(msg.jobs)
		m.reconcileSelection()
		if m.autoMode && m.selectNewActiveRun() {
			m.clearFilter()
			m.stdoutFollow = true
			m.stderrFollow = true
			m.statusText = fmt.Sprintf("Auto-mode selected new active run: %s", m.selectedJobName())
			m.statusErr = false
		}
		m.rememberRunIDs()
		m.refreshJobsViewport(false)
		if job := m.selectedJob(); job != nil {
			m.loadInputContent()
			cmds = append(cmds, loadLogsCmd(job.RunID, job.RunRoot))
		} else {
			m.stdoutContent = ""
			m.stderrContent = ""
			m.inputContent = ""
			m.refreshViewports()
		}
	case logsMsg:
		if msg.err != nil {
			m.statusErr = true
			m.statusText = msg.err.Error()
			break
		}
		job := m.selectedJob()
		if job == nil || job.RunID != msg.runID {
			break
		}
		m.fullStdout = strings.TrimRight(msg.stdout, "\n")
		m.fullStderr = strings.TrimRight(msg.stderr, "\n")
		if !m.filterActive {
			m.stdoutContent = m.fullStdout
			m.stderrContent = m.fullStderr
		}
		m.refreshLogViewports()
	case actionMsg:
		m.busy = false
		m.busyAction = ""
		m.statusErr = msg.err != nil
		if msg.err != nil {
			m.statusText = msg.err.Error()
		} else if msg.statusMsg != "" {
			m.statusText = msg.statusMsg
		} else {
			m.statusText = fmt.Sprintf("%s completed", msg.name)
		}
		if msg.filter != nil {
			m.applyGrepFilter(*msg.filter)
		} else if msg.raw != "" {
			m.modalOpen = true
			m.modalTitle = msg.name
			m.setModalContent(msg.raw)
			m.focus = focusModal
		}
		if msg.runID != "" {
			m.selectedID = msg.runID
		}
		m.cancelCommand()
		cmds = append(cmds, loadJobsCmd(m.runner, m.scopeDir))
	case tea.MouseMsg:
		return m.handleMouse(msg)
	case tea.KeyMsg:
		return m.handleKey(msg)
	}

	return m, tea.Batch(cmds...)
}

func (m model) handleMouse(msg tea.MouseMsg) (tea.Model, tea.Cmd) {
	switch msg.Button {
	case tea.MouseButtonWheelUp:
		m.scrollFocused(-3, msg.X, msg.Y)
		return m, nil
	case tea.MouseButtonWheelDown:
		m.scrollFocused(3, msg.X, msg.Y)
		return m, nil
	}

	switch msg.Action {
	case tea.MouseActionPress:
		if m.modalOpen {
			m.focus = focusModal
			return m, nil
		}
		if m.filterActive {
			m.clearFilter()
		}
		for _, btn := range m.headerButtons {
			if btn.rect.contains(msg.X, msg.Y) {
				m.togglePanel(btn.name)
				return m, nil
			}
		}
		if m.inputVisible && m.inputCloseRect.contains(msg.X, msg.Y) {
			m.togglePanel("Input")
			return m, nil
		}
		if m.stdoutVisible && m.stdoutCloseRect.contains(msg.X, msg.Y) {
			m.togglePanel("Stdout")
			return m, nil
		}
		if m.stderrVisible && m.stderrCloseRect.contains(msg.X, msg.Y) {
			m.togglePanel("Stderr")
			return m, nil
		}
		if m.outputDividerHit(msg.X, msg.Y) {
			m.draggingDivider = true
			m.resizeOutputSplitFromX(msg.X)
			m.statusErr = false
			m.statusText = "Dragging stdout/stderr split."
			return m, nil
		}
		if m.jobsRect.contains(msg.X, msg.Y) {
			m.focus = focusJobs
			row := msg.Y - (m.jobsRect.y + 2) + m.jobsVP.YOffset
			if row >= 0 && row < len(m.jobs) {
				m.selectedIdx = row
				m.selectedID = m.jobs[row].RunID
				m.clearFilter()
				m.refreshJobsViewport(true)
				m.stdoutFollow = true
				m.stderrFollow = true
				if cmd := m.reloadSelectedLogs(); cmd != nil {
					return m, cmd
				}
			}
			return m, nil
		}
		for i, r := range m.actionRects {
			if r.contains(msg.X, msg.Y) {
				m.actionIdx = i
				m.syncPromptMeta()
				m.focus = focusPrompt
				if m.busy {
					return m, nil
				}
				return m, m.openAction()
			}
		}
		if m.promptRect.contains(msg.X, msg.Y) {
			m.focus = focusPrompt
			m.prompt.Focus()
			return m, nil
		}
		if m.inputVisible && m.inputRect.contains(msg.X, msg.Y) {
			m.focus = focusInput
			m.beginSelection(focusInput, msg.X, msg.Y)
			return m, nil
		}
		if m.stdoutVisible && m.stdoutRect.contains(msg.X, msg.Y) {
			m.focus = focusStdout
			m.beginSelection(focusStdout, msg.X, msg.Y)
			return m, nil
		}
		if m.stderrVisible && m.stderrRect.contains(msg.X, msg.Y) {
			m.focus = focusStderr
			m.beginSelection(focusStderr, msg.X, msg.Y)
			return m, nil
		}
	case tea.MouseActionMotion:
		if m.draggingDivider {
			m.resizeOutputSplitFromX(msg.X)
			return m, nil
		}
		if m.selecting {
			m.updateSelection(msg.X, msg.Y)
			m.refreshLogViewports()
			return m, nil
		}
	case tea.MouseActionRelease:
		if m.draggingDivider {
			m.draggingDivider = false
			m.statusErr = false
			m.statusText = fmt.Sprintf("Output split set to %d%% stdout / %d%% stderr.", int(m.outputSplit*100), int((1-m.outputSplit)*100))
			return m, nil
		}
		if m.selecting {
			m.updateSelection(msg.X, msg.Y)
			m.finishSelection()
			m.refreshLogViewports()
			return m, nil
		}
	}

	return m, nil
}

// selectionPaneInfo returns the rect and viewport for the given selectable pane.
func (m *model) selectionPaneInfo(pane paneFocus) (rect, *viewport.Model, string) {
	switch pane {
	case focusInput:
		return m.inputRect, &m.inputVP, m.inputContent
	case focusStdout:
		return m.stdoutRect, &m.stdoutVP, m.stdoutContent
	case focusStderr:
		return m.stderrRect, &m.stderrVP, m.stderrContent
	}
	return rect{}, nil, ""
}

// paneWrappedLines reproduces the exact line slice the viewport renders from,
// so screen coordinates can be mapped to characters precisely.
func (m *model) paneWrappedLines(pane paneFocus) []string {
	_, vp, content := m.selectionPaneInfo(pane)
	if vp == nil {
		return nil
	}
	wrapped := wrapViewportContent(content, vp.Width)
	return strings.Split(wrapped, "\n")
}

// pointToCell maps an absolute screen coordinate to a (line, col) index into the
// pane's wrapped content. The coordinate is clamped into the content rectangle so
// dragging past an edge keeps selecting the nearest cell.
func (m *model) pointToCell(pane paneFocus, x, y int) (line, col int) {
	r, vp, _ := m.selectionPaneInfo(pane)
	if vp == nil {
		return 0, 0
	}
	contentTop := r.y + 2
	contentLeft := r.x + 2
	contentH := panelViewportHeight(r.h)

	relY := clampInt(y-contentTop, 0, max(0, contentH-1))
	line = vp.YOffset + relY

	lines := m.paneWrappedLines(pane)
	if len(lines) == 0 {
		return 0, 0
	}
	line = clampInt(line, 0, len(lines)-1)

	// Column is measured in visible cells; strip ANSI before width math.
	plain := ansi.Strip(lines[line])
	maxCol := len([]rune(plain))
	col = clampInt(x-contentLeft, 0, maxCol)
	return line, col
}

func (m *model) beginSelection(pane paneFocus, x, y int) {
	line, col := m.pointToCell(pane, x, y)
	m.selecting = true
	m.selectPane = pane
	m.selAnchorLine = line
	m.selAnchorCol = col
	m.selEndLine = line
	m.selEndCol = col
	m.refreshLogViewports()
}

func (m *model) updateSelection(x, y int) {
	if !m.selecting {
		return
	}
	m.autoScrollSelection(y)
	line, col := m.pointToCell(m.selectPane, x, y)
	m.selEndLine = line
	m.selEndCol = col
}

// autoScrollSelection scrolls the active selection pane when the drag cursor
// reaches (or passes) the top or bottom content edge, so the user can extend a
// selection beyond the currently visible lines. Scroll speed grows with how far
// past the edge the cursor is.
func (m *model) autoScrollSelection(y int) {
	r, vp, _ := m.selectionPaneInfo(m.selectPane)
	if vp == nil {
		return
	}
	contentTop := r.y + 2
	contentH := panelViewportHeight(r.h)
	contentBottom := contentTop + contentH - 1
	maxOffset := max(0, vp.TotalLineCount()-vp.Height)

	switch {
	case y > contentBottom:
		if vp.YOffset >= maxOffset {
			return
		}
		step := clampInt(y-contentBottom, 1, 5)
		vp.YOffset = clampInt(vp.YOffset+step, 0, maxOffset)
		// Following content would fight the user's scroll; pin it off.
		if m.selectPane == focusStdout {
			m.stdoutFollow = false
		} else if m.selectPane == focusStderr {
			m.stderrFollow = false
		}
	case y < contentTop:
		if vp.YOffset <= 0 {
			return
		}
		step := clampInt(contentTop-y, 1, 5)
		vp.YOffset = clampInt(vp.YOffset-step, 0, maxOffset)
		if m.selectPane == focusStdout {
			m.stdoutFollow = false
		} else if m.selectPane == focusStderr {
			m.stderrFollow = false
		}
	}
}

// orderedSelection returns the selection bounds normalized so start <= end.
func (m *model) orderedSelection() (startLine, startCol, endLine, endCol int) {
	startLine, startCol = m.selAnchorLine, m.selAnchorCol
	endLine, endCol = m.selEndLine, m.selEndCol
	if startLine > endLine || (startLine == endLine && startCol > endCol) {
		startLine, startCol, endLine, endCol = endLine, endCol, startLine, startCol
	}
	return
}

// selectedText extracts the currently selected substring from the pane's
// wrapped content, joining multi-line selections with newlines.
func (m *model) selectedText() string {
	lines := m.paneWrappedLines(m.selectPane)
	if len(lines) == 0 {
		return ""
	}
	startLine, startCol, endLine, endCol := m.orderedSelection()
	startLine = clampInt(startLine, 0, len(lines)-1)
	endLine = clampInt(endLine, 0, len(lines)-1)

	var b strings.Builder
	for ln := startLine; ln <= endLine; ln++ {
		runes := []rune(ansi.Strip(lines[ln]))
		lo, hi := 0, len(runes)
		if ln == startLine {
			lo = clampInt(startCol, 0, len(runes))
		}
		if ln == endLine {
			hi = clampInt(endCol, 0, len(runes))
		}
		if lo > hi {
			lo = hi
		}
		b.WriteString(string(runes[lo:hi]))
		if ln != endLine {
			b.WriteByte('\n')
		}
	}
	return b.String()
}

func (m *model) hasSelection() bool {
	if !m.selecting {
		return false
	}
	return m.selAnchorLine != m.selEndLine || m.selAnchorCol != m.selEndCol
}

func (m *model) clearSelection() {
	m.selecting = false
	m.selAnchorLine, m.selAnchorCol = 0, 0
	m.selEndLine, m.selEndCol = 0, 0
}

func (m *model) finishSelection() {
	defer m.clearSelection()
	if !m.hasSelection() {
		return
	}
	text := m.selectedText()
	if strings.TrimSpace(text) == "" {
		return
	}
	if err := clipboard.WriteAll(text); err != nil {
		m.statusErr = true
		m.statusText = "Copy failed: " + err.Error()
		return
	}
	lineCount := strings.Count(text, "\n") + 1
	m.statusErr = false
	m.statusText = fmt.Sprintf("Copied %d chars (%d lines) to clipboard.", len([]rune(text)), lineCount)
}

// highlightSelection wraps the selected cell ranges of the pane's wrapped
// content with a reverse-video style and returns the full content string with
// highlight escapes injected, ready to hand to viewport.SetContent.
func (m *model) highlightSelection(pane paneFocus, wrapped string) string {
	if !m.selecting || m.selectPane != pane || !m.hasSelection() {
		return wrapped
	}
	lines := strings.Split(wrapped, "\n")
	startLine, startCol, endLine, endCol := m.orderedSelection()
	if startLine >= len(lines) {
		return wrapped
	}
	endLine = clampInt(endLine, 0, len(lines)-1)
	for ln := startLine; ln <= endLine; ln++ {
		runes := []rune(ansi.Strip(lines[ln]))
		lo, hi := 0, len(runes)
		if ln == startLine {
			lo = clampInt(startCol, 0, len(runes))
		}
		if ln == endLine {
			hi = clampInt(endCol, 0, len(runes))
		}
		if lo >= hi {
			continue
		}
		before := string(runes[:lo])
		sel := string(runes[lo:hi])
		after := string(runes[hi:])
		lines[ln] = before + selectionStyle.Render(sel) + after
	}
	return strings.Join(lines, "\n")
}

// togglePanel flips the visibility of the named main panel (Input, Stdout,
// Stderr) and relayouts. Jobs is always on and ignored. When a panel becomes
// visible its content is refreshed; when it hides, focus that pointed at it
// falls back to jobs. Unlike the stdout/stderr-only guard that kept at least
// one of those two open, all three main panels may close at once (jobs always
// remains).
func (m *model) togglePanel(name string) {
	switch name {
	case "Input":
		m.inputVisible = !m.inputVisible
		if m.inputVisible {
			m.loadInputContent()
		} else if m.focus == focusInput {
			m.focus = focusJobs
		}
	case "Stdout":
		m.stdoutVisible = !m.stdoutVisible
		if !m.stdoutVisible && m.focus == focusStdout {
			m.focus = focusJobs
		}
	case "Stderr":
		m.stderrVisible = !m.stderrVisible
		if !m.stderrVisible && m.focus == focusStderr {
			m.focus = focusJobs
		}
	default:
		return
	}
	m.layout()
	m.refreshViewports()
}

// focusChain returns the left-to-right ordered list of focusable columns,
// skipping hidden main panels. Jobs is always present at the head and prompt
// is always the tail, so left/right navigation can walk this slice.
func (m model) focusChain() []paneFocus {
	chain := []paneFocus{focusJobs}
	if m.inputVisible {
		chain = append(chain, focusInput)
	}
	if m.stdoutVisible {
		chain = append(chain, focusStdout)
	}
	if m.stderrVisible {
		chain = append(chain, focusStderr)
	}
	chain = append(chain, focusPrompt)
	return chain
}

func (m model) focusLeftOf(cur paneFocus) paneFocus {
	chain := m.focusChain()
	for i, f := range chain {
		if f == cur {
			if i > 0 {
				return chain[i-1]
			}
			return cur
		}
	}
	return focusJobs
}

func (m model) focusRightOf(cur paneFocus) paneFocus {
	chain := m.focusChain()
	for i, f := range chain {
		if f == cur {
			if i < len(chain)-1 {
				return chain[i+1]
			}
			return cur
		}
	}
	return focusPrompt
}

func (m model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	if m.modalOpen {
		switch msg.String() {
		case "esc", "q":
			m.modalOpen = false
			m.focus = focusPrompt
			return m, nil
		case "up", "k":
			m.modalVP.LineUp(1)
			return m, nil
		case "down", "j":
			m.modalVP.LineDown(1)
			return m, nil
		case "pgup":
			m.modalVP.HalfViewUp()
			return m, nil
		case "pgdown":
			m.modalVP.HalfViewDown()
			return m, nil
		}
	}
	if m.filterActive && msg.String() == "esc" {
		m.clearFilter()
		m.statusErr = false
		m.statusText = "Grep results cleared."
		return m, nil
	}
	if m.commandMode {
		return m.handleCommandKey(msg)
	}

	switch msg.String() {
	case "ctrl+c", "q":
		return m, tea.Quit
	case "1":
		m.togglePanel("Stdout")
		return m, nil
	case "2":
		m.togglePanel("Stderr")
		return m, nil
	case "3":
		m.togglePanel("Input")
		return m, nil
	case "[":
		m.outputSplit -= 0.05
		m.outputSplit = clampOutputSplit(m.outputSplit)
		m.layout()
		m.refreshViewports()
		return m, nil
	case "]":
		m.outputSplit += 0.05
		m.outputSplit = clampOutputSplit(m.outputSplit)
		m.layout()
		m.refreshViewports()
		return m, nil
	case "a":
		m.autoMode = !m.autoMode
		if m.autoMode {
			m.statusText = "Auto-mode enabled: will jump when a new active runner starts."
		} else {
			m.statusText = "Auto-mode disabled: selection stays where you put it."
		}
		m.statusErr = false
		return m, nil
	case "tab":
		m.actionIdx = (m.actionIdx + 1) % len(m.actions)
		m.syncPromptMeta()
		m.focus = focusPrompt
		return m, nil
	case "shift+tab":
		m.actionIdx--
		if m.actionIdx < 0 {
			m.actionIdx = len(m.actions) - 1
		}
		m.syncPromptMeta()
		m.focus = focusPrompt
		return m, nil
	case "enter":
		if m.busy {
			return m, nil
		}
		return m, m.openAction()
	case "esc":
		if m.filterActive {
			m.clearFilter()
			m.statusText = "Grep results cleared."
			m.statusErr = false
		} else {
			m.cancelCommand()
			m.statusText = "Command cancelled."
			m.statusErr = false
		}
		return m, nil
	case "r":
		return m, tea.Batch(loadJobsCmd(m.runner, m.scopeDir), m.reloadSelectedLogs())
	case "up", "k":
		switch m.focus {
		case focusJobs:
			if m.selectedIdx > 0 {
				m.selectedIdx--
				m.selectedID = m.jobs[m.selectedIdx].RunID
				m.clearFilter()
				m.refreshJobsViewport(true)
				m.stdoutFollow = true
				m.stderrFollow = true
				return m, m.reloadSelectedLogs()
			}
		case focusInput:
			m.inputVP.LineUp(1)
		case focusStdout:
			m.stdoutVP.LineUp(1)
			m.stdoutFollow = m.isViewportNearBottom(m.stdoutVP)
		case focusStderr:
			m.stderrVP.LineUp(1)
			m.stderrFollow = m.isViewportNearBottom(m.stderrVP)
		default:
			if len(m.jobs) > 0 {
				m.focus = focusJobs
			}
		}
		return m, nil
	case "down", "j":
		switch m.focus {
		case focusJobs:
			if m.selectedIdx >= 0 && m.selectedIdx < len(m.jobs)-1 {
				m.selectedIdx++
				m.selectedID = m.jobs[m.selectedIdx].RunID
				m.clearFilter()
				m.refreshJobsViewport(true)
				m.stdoutFollow = true
				m.stderrFollow = true
				return m, m.reloadSelectedLogs()
			}
		case focusInput:
			m.inputVP.LineDown(1)
		case focusStdout:
			m.stdoutVP.LineDown(1)
			m.stdoutFollow = m.isViewportNearBottom(m.stdoutVP)
		case focusStderr:
			m.stderrVP.LineDown(1)
			m.stderrFollow = m.isViewportNearBottom(m.stderrVP)
		}
		return m, nil
	case "left", "h":
		m.focus = m.focusLeftOf(m.focus)
		return m, nil
	case "right", "l":
		m.focus = m.focusRightOf(m.focus)
		return m, nil
	case "pgup":
		if m.focus == focusInput {
			m.inputVP.HalfViewUp()
		}
		if m.focus == focusStdout {
			m.stdoutVP.HalfViewUp()
			m.stdoutFollow = m.isViewportNearBottom(m.stdoutVP)
		}
		if m.focus == focusStderr {
			m.stderrVP.HalfViewUp()
			m.stderrFollow = m.isViewportNearBottom(m.stderrVP)
		}
		return m, nil
	case "pgdown":
		if m.focus == focusInput {
			m.inputVP.HalfViewDown()
		}
		if m.focus == focusStdout {
			m.stdoutVP.HalfViewDown()
			m.stdoutFollow = m.isViewportNearBottom(m.stdoutVP)
		}
		if m.focus == focusStderr {
			m.stderrVP.HalfViewDown()
			m.stderrFollow = m.isViewportNearBottom(m.stderrVP)
		}
		return m, nil
	}

	return m, nil
}

func (m model) handleCommandKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "ctrl+c":
		return m, tea.Quit
	case "esc":
		m.cancelCommand()
		m.statusText = "Command cancelled."
		m.statusErr = false
		return m, nil
	case "tab":
		m.nextField()
		return m, nil
	case "shift+tab":
		m.previousField()
		return m, nil
	case "enter":
		if m.busy {
			return m, nil
		}
		return m, m.submitAction()
	case "up", "k":
		m.adjustEnum(-1)
		return m, nil
	case "down", "j":
		m.adjustEnum(1)
		return m, nil
	}

	if m.activeFieldKind() == fieldText {
		var cmd tea.Cmd
		m.fields[m.fieldIdx].input, cmd = m.fields[m.fieldIdx].input.Update(msg)
		return m, cmd
	}
	return m, nil
}

func (m *model) openAction() tea.Cmd {
	action := m.actions[m.actionIdx]
	selected := m.selectedJob()

	if action.NeedsSelection && selected == nil {
		m.statusErr = true
		m.statusText = fmt.Sprintf("%s needs a selected run.", action.Name)
		return nil
	}
	m.beginCommand(action)
	if len(m.fields) == 0 {
		return m.submitAction()
	}
	m.statusErr = false
	m.statusText = fmt.Sprintf("Editing %s. Tab changes fields, Enter submits, Esc cancels.", action.Name)
	return nil
}

func (m *model) submitAction() tea.Cmd {
	action := m.actions[m.actionIdx]
	selected := m.selectedJob()
	values := m.commandValues()
	for _, field := range m.fields {
		if field.spec.Required && strings.TrimSpace(values[field.spec.Name]) == "" {
			m.statusErr = true
			m.statusText = fmt.Sprintf("%s is required.", field.spec.Label)
			return nil
		}
	}

	m.busy = true
	m.busyAction = action.Name
	m.statusErr = false
	m.statusText = fmt.Sprintf("Running %s…", action.Name)
	m.commandMode = false
	return actionCmd(m.runner, m.scopeDir, action, selected, values)
}

func (m *model) beginCommand(action appAction) {
	m.commandMode = true
	m.focus = focusPrompt
	m.fieldIdx = 0
	m.fields = make([]commandField, 0, len(action.Fields))
	for _, spec := range action.Fields {
		field := commandField{spec: spec}
		if spec.Kind == fieldText {
			input := textinput.New()
			input.Prompt = ""
			input.CharLimit = 0
			input.Placeholder = spec.Placeholder
			input.SetValue(spec.Default)
			field.input = input
		}
		if spec.Kind == fieldEnum {
			for i, option := range spec.Options {
				if option == spec.Default {
					field.enumIndex = i
					break
				}
			}
		}
		m.fields = append(m.fields, field)
	}
	m.focusActiveField()
}

func (m *model) cancelCommand() {
	m.commandMode = false
	m.fields = nil
	m.fieldIdx = 0
	m.prompt.SetValue("")
	m.syncPromptMeta()
}

func (m *model) applyGrepFilter(filter grepFilter) {
	m.filterActive = true
	m.filterTitle = fmt.Sprintf("grep %q", filter.pattern)
	m.stdoutContent = filter.stdout
	m.stderrContent = filter.stderr
	m.stdoutFollow = false
	m.stderrFollow = false
	m.stdoutVP.GotoTop()
	m.stderrVP.GotoTop()
	truncated := ""
	if filter.truncated {
		truncated = " truncated"
	}
	m.statusErr = false
	m.statusText = fmt.Sprintf("Grep matched %d lines (%d stdout, %d stderr)%s. Esc restores full logs.", filter.totalMatches, filter.stdoutMatches, filter.stderrMatches, truncated)
	m.refreshLogViewports()
}

func (m *model) clearFilter() {
	if !m.filterActive {
		return
	}
	m.filterActive = false
	m.filterTitle = ""
	m.stdoutContent = m.fullStdout
	m.stderrContent = m.fullStderr
	m.stdoutFollow = true
	m.stderrFollow = true
	m.refreshLogViewports()
}

func (m *model) focusActiveField() {
	for i := range m.fields {
		if m.fields[i].spec.Kind == fieldText {
			if i == m.fieldIdx {
				m.fields[i].input.Focus()
			} else {
				m.fields[i].input.Blur()
			}
		}
	}
}

func (m *model) nextField() {
	if len(m.fields) == 0 {
		return
	}
	m.fieldIdx = (m.fieldIdx + 1) % len(m.fields)
	m.focusActiveField()
}

func (m *model) previousField() {
	if len(m.fields) == 0 {
		return
	}
	m.fieldIdx--
	if m.fieldIdx < 0 {
		m.fieldIdx = len(m.fields) - 1
	}
	m.focusActiveField()
}

func (m *model) adjustEnum(delta int) {
	if len(m.fields) == 0 || m.fields[m.fieldIdx].spec.Kind != fieldEnum {
		return
	}
	options := m.fields[m.fieldIdx].spec.Options
	if len(options) == 0 {
		return
	}
	idx := (m.fields[m.fieldIdx].enumIndex + delta) % len(options)
	if idx < 0 {
		idx = len(options) - 1
	}
	m.fields[m.fieldIdx].enumIndex = idx
}

func (m model) activeFieldKind() fieldKind {
	if len(m.fields) == 0 || m.fieldIdx < 0 || m.fieldIdx >= len(m.fields) {
		return fieldText
	}
	return m.fields[m.fieldIdx].spec.Kind
}

func (m model) commandValues() map[string]string {
	values := make(map[string]string, len(m.fields))
	for _, field := range m.fields {
		switch field.spec.Kind {
		case fieldText:
			values[field.spec.Name] = strings.TrimSpace(field.input.Value())
		case fieldEnum:
			if len(field.spec.Options) > 0 && field.enumIndex >= 0 && field.enumIndex < len(field.spec.Options) {
				values[field.spec.Name] = field.spec.Options[field.enumIndex]
			}
		}
	}
	return values
}

func (m *model) scrollFocused(delta, x, y int) {
	if m.modalOpen {
		if delta < 0 {
			m.modalVP.LineUp(-delta)
		} else {
			m.modalVP.LineDown(delta)
		}
		return
	}
	switch {
	case m.jobsRect.contains(x, y):
		if delta < 0 {
			m.jobsVP.LineUp(-delta)
			m.jobsPinnedBottom = false
		} else {
			m.jobsVP.LineDown(delta)
			m.jobsPinnedBottom = m.isViewportNearBottom(m.jobsVP)
		}
		m.focus = focusJobs
	case m.inputRect.contains(x, y):
		if delta < 0 {
			m.inputVP.LineUp(-delta)
		} else {
			m.inputVP.LineDown(delta)
		}
		m.focus = focusInput
	case m.stdoutRect.contains(x, y):
		if delta < 0 {
			m.stdoutVP.LineUp(-delta)
		} else {
			m.stdoutVP.LineDown(delta)
		}
		m.stdoutFollow = m.isViewportNearBottom(m.stdoutVP)
		m.focus = focusStdout
	case m.stderrRect.contains(x, y):
		if delta < 0 {
			m.stderrVP.LineUp(-delta)
		} else {
			m.stderrVP.LineDown(delta)
		}
		m.stderrFollow = m.isViewportNearBottom(m.stderrVP)
		m.focus = focusStderr
	}
}

func (m *model) reloadSelectedLogs() tea.Cmd {
	job := m.selectedJob()
	if job == nil {
		m.inputContent = ""
		return nil
	}
	m.loadInputContent()
	m.refreshLogViewports()
	return loadLogsCmd(job.RunID, job.RunRoot)
}

// loadInputContent populates m.inputContent with the original command or
// prompt for the selected run, read directly from the run dir (same pattern
// loadLogsCmd uses to read stdout.log/stderr.log from RunRoot). For a normal
// run this is meta.json's "cmd". For a sub-agent run (meta has agentRuntime)
// it's the current turn's prompt.md, falling back to meta cmd when prompt.md
// is absent. This is a cheap synchronous read of a few hundred bytes, so it
// runs inline on selection rather than through a tea.Cmd.
func (m *model) loadInputContent() {
	job := m.selectedJob()
	if job == nil {
		m.inputContent = ""
		return
	}
	m.inputContent = readInputContent(job.RunRoot)
}

func readInputContent(runRoot string) string {
	if runRoot == "" {
		return ""
	}
	meta := readRunMeta(runRoot)
	if meta != nil && meta.AgentRuntime != "" {
		promptPath := filepath.Join(runRoot, "prompt.md")
		if data, err := os.ReadFile(promptPath); err == nil {
			if text := strings.TrimRight(string(data), "\n"); text != "" {
				return text
			}
		}
	}
	if meta != nil && meta.Cmd != "" {
		return meta.Cmd
	}
	return ""
}

func readRunMeta(runRoot string) *runMeta {
	data, err := os.ReadFile(filepath.Join(runRoot, "meta.json"))
	if err != nil {
		return nil
	}
	var meta runMeta
	if err := json.Unmarshal(data, &meta); err != nil {
		return nil
	}
	return &meta
}

func (m *model) selectedJob() *runSummary {
	if m.selectedIdx < 0 || m.selectedIdx >= len(m.jobs) {
		return nil
	}
	return &m.jobs[m.selectedIdx]
}

func (m *model) reconcileSelection() {
	if len(m.jobs) == 0 {
		m.selectedIdx = -1
		m.selectedID = ""
		return
	}
	if m.selectedID != "" {
		for i := range m.jobs {
			if m.jobs[i].RunID == m.selectedID {
				m.selectedIdx = i
				return
			}
		}
	}
	if m.selectedIdx < 0 || m.selectedIdx >= len(m.jobs) {
		m.selectedIdx = len(m.jobs) - 1
	}
	m.selectedID = m.jobs[m.selectedIdx].RunID
}

func (m *model) selectNewActiveRun() bool {
	if !m.runIDsReady || len(m.jobs) == 0 {
		return false
	}

	newestIdx := -1
	for i, job := range m.jobs {
		if !isActiveRun(job) {
			continue
		}
		if _, seen := m.knownRunIDs[job.RunID]; seen {
			continue
		}
		if newestIdx == -1 || job.StartedAt > m.jobs[newestIdx].StartedAt {
			newestIdx = i
		}
	}
	if newestIdx == -1 {
		return false
	}

	m.selectedIdx = newestIdx
	m.selectedID = m.jobs[newestIdx].RunID
	return true
}

func (m *model) rememberRunIDs() {
	if m.knownRunIDs == nil {
		m.knownRunIDs = make(map[string]struct{}, len(m.jobs))
	}
	for _, job := range m.jobs {
		m.knownRunIDs[job.RunID] = struct{}{}
	}
	m.runIDsReady = true
}

func (m model) selectedJobName() string {
	job := m.selectedJob()
	if job == nil {
		return ""
	}
	return job.Name
}

func isActiveRun(job runSummary) bool {
	return job.State != "exited"
}

func (m model) outputDividerHit(x, y int) bool {
	if !m.stdoutVisible || !m.stderrVisible || m.dividerRect.h <= 0 {
		return false
	}
	hit := m.dividerRect
	hit.x--
	hit.w += 2
	return hit.contains(x, y)
}

func (m *model) resizeOutputSplitFromX(x int) {
	if !m.stdoutVisible || !m.stderrVisible {
		return
	}
	// The split is measured within the stdout/stderr region, which begins
	// at the stdout panel's x and spans to the right edge. When the Input
	// panel is visible it occupies a column to the left, so origin/width
	// derive from the live stdout rect rather than the whole main area.
	restX := m.stdoutRect.x
	restW := m.width - restX
	if restW <= 41 {
		return
	}
	m.outputSplit = clampOutputSplit(float64(x-restX) / float64(restW-1))
	m.layout()
	m.refreshViewports()
}

func clampOutputSplit(ratio float64) float64 {
	if ratio < 0.25 {
		return 0.25
	}
	if ratio > 0.75 {
		return 0.75
	}
	return ratio
}

func (m *model) syncPromptMeta() {
	action := m.actions[m.actionIdx]
	m.prompt.Placeholder = action.Placeholder
}

func (m *model) setModalContent(raw string) {
	if m.modalVP.Width <= 0 || m.modalVP.Height <= 0 {
		m.modalVP = viewport.New(0, 0)
	}
	m.modalVP.SetContent(raw)
	m.modalVP.GotoTop()
}

func (m *model) layout() {
	if m.width <= 0 || m.height <= 0 {
		return
	}
	headerH := 3
	footerH := 8
	bodyY := headerH
	bodyH := m.height - headerH - footerH
	if bodyH < 6 {
		bodyH = 6
	}

	jobsW := clampInt(max(28, m.width/4), 28, 42)
	if jobsW > m.width-30 {
		jobsW = max(24, m.width/3)
	}
	mainW := max(20, m.width-jobsW-1)

	m.jobsRect = rect{x: 0, y: bodyY, w: jobsW, h: bodyH}
	mainX := jobsW + 1

	// Reset every main panel rect; the branches below only set the visible
	// ones, so hidden panels keep a zero rect and never hit-test.
	m.inputRect = rect{}
	m.stdoutRect = rect{}
	m.stderrRect = rect{}
	m.dividerRect = rect{}

	// The Input panel is an equal-width column to the LEFT of the
	// stdout/stderr pair. We carve it off first, then run the existing
	// stdout/stderr split logic on the remaining width so the divider drag
	// behavior is preserved. Column count = number of visible main panels
	// (input counts as one; stdout+stderr together count as one or two).
	restX := mainX
	restW := mainW
	if m.inputVisible {
		visibleCols := 1 // input
		if m.stdoutVisible {
			visibleCols++
		}
		if m.stderrVisible {
			visibleCols++
		}
		if m.stdoutVisible || m.stderrVisible {
			inputW := mainW / visibleCols
			inputW = clampInt(inputW, 20, mainW-21)
			m.inputRect = rect{x: mainX, y: bodyY, w: inputW, h: bodyH}
			restX = mainX + inputW + 1
			restW = mainW - inputW - 1
		} else {
			// Input is the only visible main panel: full width.
			m.inputRect = rect{x: mainX, y: bodyY, w: mainW, h: bodyH}
			restX = mainX
			restW = 0
		}
	}

	if m.stdoutVisible && m.stderrVisible {
		stdoutW := int(float64(restW-1) * m.outputSplit)
		stdoutW = clampInt(stdoutW, 20, restW-21)
		stderrW := restW - stdoutW - 1
		m.stdoutRect = rect{x: restX, y: bodyY, w: stdoutW, h: bodyH}
		m.dividerRect = rect{x: restX + stdoutW, y: bodyY, w: 1, h: bodyH}
		m.stderrRect = rect{x: restX + stdoutW + 1, y: bodyY, w: stderrW, h: bodyH}
	} else if m.stdoutVisible {
		m.stdoutRect = rect{x: restX, y: bodyY, w: restW, h: bodyH}
	} else if m.stderrVisible {
		m.stderrRect = rect{x: restX, y: bodyY, w: restW, h: bodyH}
	}

	m.promptRect = rect{x: 0, y: bodyY + bodyH, w: m.width, h: footerH}
	m.prompt.Width = max(12, m.width-26)
	for i := range m.fields {
		if m.fields[i].spec.Kind == fieldText {
			m.fields[i].input.Width = max(12, m.width-34)
		}
	}
	m.layoutActionRects()
	m.layoutHeaderButtons()
	m.layoutCloseButtons()
	m.resizeViewport(&m.jobsVP, panelContentWidth(m.jobsRect.w), panelViewportHeight(m.jobsRect.h))
	if m.inputVisible {
		m.resizeViewport(&m.inputVP, panelContentWidth(m.inputRect.w), panelViewportHeight(m.inputRect.h))
	}
	if m.stdoutVisible {
		m.resizeViewport(&m.stdoutVP, panelContentWidth(m.stdoutRect.w), panelViewportHeight(m.stdoutRect.h))
	}
	if m.stderrVisible {
		m.resizeViewport(&m.stderrVP, panelContentWidth(m.stderrRect.w), panelViewportHeight(m.stderrRect.h))
	}

	modalW := clampInt(m.width-12, 40, 120)
	modalH := clampInt(m.height-8, 12, 40)
	m.resizeViewport(&m.modalVP, modalW-4, modalH-4)
}

func (m *model) resizeViewport(vp *viewport.Model, width, height int) {
	if width < 0 {
		width = 0
	}
	if height < 0 {
		height = 0
	}
	vp.Width = width
	vp.Height = height
}

func (m *model) layoutActionRects() {
	buttonX := 2
	buttonY := m.promptRect.y + 3
	m.actionRects = make([]rect, 0, len(m.actions))
	for i, action := range m.actions {
		btnW := lipgloss.Width(actionButton(action.Name, i == m.actionIdx))
		m.actionRects = append(m.actionRects, rect{x: buttonX, y: buttonY, w: btnW, h: 1})
		buttonX += btnW + 1
	}
}

func (m *model) refreshViewports() {
	m.refreshJobsViewport(false)
	m.refreshLogViewports()
}

func (m *model) refreshLogViewports() {
	if m.inputVisible {
		prev := m.inputVP.YOffset
		wrapped := wrapViewportContent(m.inputContent, m.inputVP.Width)
		m.inputVP.SetContent(m.highlightSelection(focusInput, wrapped))
		m.inputVP.YOffset = clampInt(prev, 0, max(0, m.inputVP.TotalLineCount()-m.inputVP.Height))
	}
	if m.stdoutVisible {
		prev := m.stdoutVP.YOffset
		wrapped := wrapViewportContent(m.stdoutContent, m.stdoutVP.Width)
		m.stdoutVP.SetContent(m.highlightSelection(focusStdout, wrapped))
		if m.stdoutFollow {
			m.stdoutVP.GotoBottom()
		} else {
			m.stdoutVP.YOffset = clampInt(prev, 0, max(0, m.stdoutVP.TotalLineCount()-m.stdoutVP.Height))
		}
	}
	if m.stderrVisible {
		prev := m.stderrVP.YOffset
		wrapped := wrapViewportContent(m.stderrContent, m.stderrVP.Width)
		m.stderrVP.SetContent(m.highlightSelection(focusStderr, wrapped))
		if m.stderrFollow {
			m.stderrVP.GotoBottom()
		} else {
			m.stderrVP.YOffset = clampInt(prev, 0, max(0, m.stderrVP.TotalLineCount()-m.stderrVP.Height))
		}
	}
}

func (m *model) refreshJobsViewport(ensureSelectionVisible bool) {
	var lines []string
	for i, job := range m.jobs {
		lines = append(lines, m.renderJobLine(i, job))
	}
	if len(lines) == 0 {
		lines = []string{"No runner jobs in this scope yet."}
	}
	prev := m.jobsVP.YOffset
	m.jobsVP.SetContent(strings.Join(lines, "\n"))
	maxOffset := max(0, len(lines)-m.jobsVP.Height)
	if ensureSelectionVisible && m.selectedIdx >= 0 && m.selectedIdx < len(m.jobs) {
		if m.selectedIdx < prev {
			m.jobsVP.YOffset = m.selectedIdx
		} else if m.selectedIdx >= prev+m.jobsVP.Height {
			m.jobsVP.YOffset = m.selectedIdx - m.jobsVP.Height + 1
		} else {
			m.jobsVP.YOffset = clampInt(prev, 0, maxOffset)
		}
		m.jobsPinnedBottom = m.isViewportNearBottom(m.jobsVP)
	} else if m.jobsPinnedBottom {
		m.jobsVP.GotoBottom()
	} else {
		m.jobsVP.YOffset = clampInt(prev, 0, maxOffset)
	}
}

func (m model) isViewportNearBottom(vp viewport.Model) bool {
	maxOffset := max(0, vp.TotalLineCount()-vp.Height)
	return vp.YOffset >= maxOffset-1
}

func wrapViewportContent(content string, width int) string {
	if width <= 0 || content == "" {
		return content
	}
	return ansi.Hardwrap(normalizeLogForViewport(content), width, true)
}

func normalizeLogForViewport(content string) string {
	// Carriage returns are the main display hazard: tools like `go mod
	// download`, skopeo/buildah, and progress bars terminate lines with a
	// bare \r (or \r\n) to overwrite in place. Rendered raw, that \r returns
	// the terminal cursor to column 0 mid-frame and corrupts the panel
	// layout. Collapse all \r\n and bare \r to \n so each update becomes its
	// own line that stays inside the panel.
	content = strings.ReplaceAll(content, "\r\n", "\n")
	content = strings.ReplaceAll(content, "\r", "\n")
	// Strip any remaining ANSI escape sequences (colors, cursor moves) so
	// nothing can reposition the terminal cursor or bleed styling across the
	// panel border.
	content = ansi.Strip(content)
	// Drop other control characters that can desync the renderer.
	content = strings.ReplaceAll(content, "\x00", "")
	content = strings.ReplaceAll(content, "\x08", "")
	return content
}

func (m model) View() string {
	if m.width == 0 || m.height == 0 {
		return "Loading runner-mcp…"
	}

	header := m.renderHeader()
	body := m.renderBody()
	footer := m.renderFooter()
	base := lipgloss.JoinVertical(lipgloss.Left, header, body, footer)

	if !m.modalOpen {
		return base
	}
	return lipgloss.Place(m.width, m.height, lipgloss.Center, lipgloss.Center, m.renderModal(), lipgloss.WithWhitespaceChars(" "))
}

func (m model) renderHeader() string {
	active, exited := 0, 0
	for _, job := range m.jobs {
		if !isActiveRun(job) {
			exited++
		} else {
			active++
		}
	}

	title := titleStyle.Render("runner-mcp")
	scope := mutedStyle.Render(trimMiddle(m.scopeDir, max(24, m.width-46)))
	autoLabel := "auto:on"
	autoStyle := chipStyle
	if !m.autoMode {
		autoLabel = "auto:off"
		autoStyle = secondaryChipStyle
	}
	stats := chipStyle.Render(fmt.Sprintf("%d active", active)) + " " + secondaryChipStyle.Render(fmt.Sprintf("%d exited", exited)) + " " + autoStyle.Render(autoLabel)
	leftLine1 := lipgloss.JoinHorizontal(lipgloss.Left, title, "  ", scope, "  ", stats)

	// Panel toggle buttons, right-justified into the empty top-right corner of
	// line 1. We compose line 1 as: left content + filler + bar, so the
	// buttons sit flush right without touching the left-side content. The
	// button hit-rects come from headerToggleBar (already right-justified).
	bar, _ := m.headerToggleBar()
	barW := lipgloss.Width(strings.TrimLeft(bar, " "))
	leftW := lipgloss.Width(leftLine1)
	avail := max(1, m.width-2) // usable width inside Padding(1,1,0,1)
	gap := avail - leftW - barW
	var line1 string
	if gap >= 1 {
		line1 = leftLine1 + strings.Repeat(" ", gap) + strings.TrimLeft(bar, " ")
	} else {
		// Not enough room to sit side by side; keep the buttons (they're the
		// interactive element) and let the left content be clipped by MaxWidth.
		line1 = lipgloss.NewStyle().MaxWidth(max(1, avail-barW-1)).Render(leftLine1) + " " + strings.TrimLeft(bar, " ")
	}

	job := m.selectedJob()
	detail := "Select a run to inspect logs and collaborate through runner."
	if job != nil {
		parts := []string{
			statusBadge(job.State, job.Result),
			boldStyle.Render(trimMiddle(job.Name, 32)),
			mutedStyle.Render(fmt.Sprintf("started %s ago", humanDuration(int(time.Now().Unix()-job.StartedAt)))),
		}
		if job.PID > 0 && job.State != "exited" {
			parts = append(parts, mutedStyle.Render(fmt.Sprintf("pid %d", job.PID)))
		}
		if len(job.Endpoints) > 0 {
			parts = append(parts, endpointStyle.Render(strings.Join(job.Endpoints, "  ")))
		}
		if job.Description != "" {
			parts = append(parts, mutedStyle.Render(trimMiddle(job.Description, 50)))
		}
		detail = strings.Join(parts, "  ")
	}
	line2 := lipgloss.NewStyle().MaxWidth(max(1, m.width-2)).Render(detail)
	return lipgloss.NewStyle().
		Width(m.width).
		Padding(1, 1, 0, 1).
		Render(line1 + "\n" + line2)
}

// panelToggles lists the header button bar entries in render order. Jobs is
// always active and not toggleable; the rest mirror panel visibility.
func (m model) panelToggles() []struct {
	name   string
	active bool
	fixed  bool
} {
	return []struct {
		name   string
		active bool
		fixed  bool
	}{
		{"Input", m.inputVisible, false},
		{"Stdout", m.stdoutVisible, false},
		{"Stderr", m.stderrVisible, false},
	}
}

// headerToggleBar renders the clickable panel-toggle button bar and returns
// the rects each button occupies on screen. The header is drawn with top
// padding 1 and left padding 1, and the bar is the third content line, so
// buttons live at screen y=3 with x starting at 1. Active panels use
// chipStyle (highlighted); inactive ones use mutedToggleStyle (disabled
// look). Keeping render and rect math in one place keeps hit-testing exact.
// headerToggleBar renders the panel open/close buttons (Jobs|Input|Stdout|
// Stderr) and returns the rendered string plus the clickable screen rects for
// each button. The bar is RIGHT-JUSTIFIED to the top-right corner of the
// header (on line 1, the title row), occupying the empty space there without
// disturbing the left-side title/scope/stats/detail content.
func (m model) headerToggleBar() (string, []headerButton) {
	const barY = 1 // top line (line 1), where the right side is empty

	toggles := m.panelToggles()
	// Build the chip strings first so we know the total bar width.
	chips := make([]string, 0, len(toggles))
	widths := make([]int, 0, len(toggles))
	total := 0
	for i, t := range toggles {
		style := mutedToggleStyle
		if t.active {
			style = chipStyle
		}
		chip := style.Render(t.name)
		w := lipgloss.Width(chip)
		chips = append(chips, chip)
		widths = append(widths, w)
		total += w
		if i < len(toggles)-1 {
			total++ // single-space gap between chips
		}
	}

	// Right edge: header has Padding(1,1,0,1), so usable content ends at
	// screen column m.width-2 (1 col right padding). Start the bar so it
	// ends there.
	startX := m.width - 1 - total
	if startX < 1 {
		startX = 1
	}

	x := startX
	buttons := make([]headerButton, 0, len(toggles))
	for i := range toggles {
		buttons = append(buttons, headerButton{name: toggles[i].name, rect: rect{x: x, y: barY, w: widths[i], h: 1}})
		x += widths[i]
		if i < len(toggles)-1 {
			x++ // the gap
		}
	}

	// Left-pad with spaces so the joined chips sit flush to the right edge.
	pad := startX - 1 // content begins at screen col 1 (after left padding)
	if pad < 0 {
		pad = 0
	}
	bar := strings.Repeat(" ", pad) + strings.Join(chips, " ")
	return bar, buttons
}

func (m *model) layoutHeaderButtons() {
	_, buttons := m.headerToggleBar()
	m.headerButtons = buttons
}

func (m model) renderBody() string {
	jobsPanel := panelStyle.Width(panelBlockWidth(m.jobsRect.w)).Height(panelBlockHeight(m.jobsRect.h)).Render(
		panelTitleStyle.Copy().Background(colorPanel).Render("Jobs") + "\n" + m.jobsVP.View(),
	)

	mainPanels := []string{jobsPanel}
	if m.inputVisible {
		title := "Input"
		if m.focus == focusInput {
			title += " • focus"
		}
		content := m.inputVP.View()
		if strings.TrimSpace(content) == "" {
			content = mutedStyle.Render("No command or prompt captured.")
		}
		titleLine := panelTitleWithClose(title, panelContentWidth(m.inputRect.w), colorPanel)
		mainPanels = append(mainPanels, inputPanelStyle.Width(panelBlockWidth(m.inputRect.w)).Height(panelBlockHeight(m.inputRect.h)).Render(titleLine+"\n"+content))
	}
	if m.stdoutVisible {
		title := "Stdout"
		if m.filterActive {
			title = "Stdout grep"
		}
		if m.focus == focusStdout {
			title += " • focus"
		}
		content := m.stdoutVP.View()
		if strings.TrimSpace(content) == "" {
			content = mutedStyle.Render("No stdout captured yet.")
		}
		titleLine := panelTitleWithClose(title, panelContentWidth(m.stdoutRect.w), colorPanel)
		mainPanels = append(mainPanels, stdoutPanelStyle.Width(panelBlockWidth(m.stdoutRect.w)).Height(panelBlockHeight(m.stdoutRect.h)).Render(titleLine+"\n"+content))
	}
	if m.stdoutVisible && m.stderrVisible {
		dividerGlyph := "│"
		divider := dividerStyle
		if m.draggingDivider {
			dividerGlyph = "┃"
			divider = divider.Foreground(colorTeal)
		}
		mainPanels = append(mainPanels, divider.Render(dividerGlyph))
	}
	if m.stderrVisible {
		title := "Stderr"
		if m.filterActive {
			title = "Stderr grep"
		}
		if m.focus == focusStderr {
			title += " • focus"
		}
		content := m.stderrVP.View()
		if strings.TrimSpace(content) == "" {
			content = mutedStyle.Render("No stderr captured yet.")
		}
		titleLine := panelTitleWithClose(title, panelContentWidth(m.stderrRect.w), colorPanel2)
		mainPanels = append(mainPanels, stderrPanelStyle.Width(panelBlockWidth(m.stderrRect.w)).Height(panelBlockHeight(m.stderrRect.h)).Render(titleLine+"\n"+content))
	}

	return lipgloss.JoinHorizontal(lipgloss.Top, mainPanels...)
}

// panelTitleWithClose renders a panel's title line with a right-aligned [x]
// close button, fitted to the panel's content width. The title is truncated
// if it would collide with the [x]. contentWidth is panelContentWidth(rect.w).
func panelTitleWithClose(title string, contentWidth int, bg lipgloss.Color) string {
	const closeLabel = "[x]"
	// Carry the panel's background through the title + close segments so the
	// title row blends into the panel instead of punching through to the
	// terminal's default (black) background.
	titleStyle := panelTitleStyle.Copy().Background(bg)
	closeStyle := closeButtonStyle.Copy().Background(bg)
	closeW := len(closeLabel)
	if contentWidth <= closeW {
		return closeStyle.Render(closeLabel)
	}
	titleRoom := contentWidth - closeW - 1
	titleText := trimRight(title, max(1, titleRoom))
	gap := contentWidth - lipgloss.Width(titleText) - closeW
	if gap < 1 {
		gap = 1
	}
	filler := lipgloss.NewStyle().Background(bg).Render(strings.Repeat(" ", gap))
	return titleStyle.Render(titleText) + filler + closeStyle.Render(closeLabel)
}

// closeButtonRect returns the screen rect of a panel's [x] close button given
// the panel's rect. The panel has a 1-cell border and 1-cell horizontal
// padding, so the content area starts at r.x+2 and the title sits on r.y+1.
// The [x] is right-aligned within the content width (3 cells wide).
func closeButtonRect(r rect) rect {
	if r.w <= 0 || r.h <= 0 {
		return rect{}
	}
	cw := panelContentWidth(r.w)
	if cw <= 3 {
		return rect{x: r.x + 2, y: r.y + 1, w: 3, h: 1}
	}
	return rect{x: r.x + 2 + (cw - 3), y: r.y + 1, w: 3, h: 1}
}

func (m *model) layoutCloseButtons() {
	if m.inputVisible {
		m.inputCloseRect = closeButtonRect(m.inputRect)
	} else {
		m.inputCloseRect = rect{}
	}
	if m.stdoutVisible {
		m.stdoutCloseRect = closeButtonRect(m.stdoutRect)
	} else {
		m.stdoutCloseRect = rect{}
	}
	if m.stderrVisible {
		m.stderrCloseRect = closeButtonRect(m.stderrRect)
	} else {
		m.stderrCloseRect = rect{}
	}
}

func (m model) renderFooter() string {
	action := m.actions[m.actionIdx]

	buttons := make([]string, 0, len(m.actions))
	for i, action := range m.actions {
		selected := i == m.actionIdx
		btn := actionButton(action.Name, selected)
		buttons = append(buttons, btn)
	}

	description := action.Description
	if action.Warning != "" {
		description = action.Warning + "  " + action.Description
	}
	if m.busy {
		description = fmt.Sprintf("Running %s…", m.busyAction)
	}

	statusLine := m.statusText
	if statusLine == "" {
		statusLine = "Tab cycles actions. Enter runs the selected action. Mouse wheel scrolls panes. Drag divider or [ ] resizes output split."
	}
	descStyle := mutedStyle
	if action.Warning != "" {
		descStyle = warningStyle
	}
	statusStyle := statusMutedStyle
	if m.statusErr {
		statusStyle = errorStyle
	}

	header := "Action Prompt"
	promptLine := promptLabelStyle.Render(action.Name) + " " + mutedStyle.Render("Enter opens command fields")
	fieldLine := descStyle.Render(trimRight(description, max(1, m.width-4)))
	commandLine := statusStyle.Render(trimRight(statusLine, max(1, m.width-4)))
	helpLine := helpStyle.Render("Tab cycles actions. Enter opens the command. a toggles auto-mode. 1/2/3 toggle stdout/stderr/input panels. Mouse clicks select actions.")
	if m.commandMode {
		header = fmt.Sprintf("Command: %s", action.Name)
		promptLine = m.renderActiveField()
		fieldLine = m.renderFieldSummary()
		commandLine = statusStyle.Render(trimRight(statusLine, max(1, m.width-4)))
		helpLine = helpStyle.Render("Tab next field  Shift+Tab previous  Up/Down enum  Enter submit  Esc cancel")
	}

	content := []string{
		panelTitleStyle.Render(header),
		promptLine,
		strings.Join(buttons, " "),
		fieldLine,
		commandLine,
		helpLine,
	}
	return footerPanelStyle.Width(panelBlockWidth(m.width)).Height(panelBlockHeight(m.promptRect.h)).Render(strings.Join(content, "\n"))
}

func (m model) renderActiveField() string {
	if len(m.fields) == 0 || m.fieldIdx < 0 || m.fieldIdx >= len(m.fields) {
		return mutedStyle.Render("No fields.")
	}
	field := m.fields[m.fieldIdx]
	label := field.spec.Label
	if field.spec.Required {
		label += "*"
	}
	prefix := promptLabelStyle.Render(label) + " "
	switch field.spec.Kind {
	case fieldText:
		return prefix + field.input.View()
	case fieldEnum:
		return prefix + m.renderEnumOptions(field)
	default:
		return prefix
	}
}

func (m model) renderEnumOptions(field commandField) string {
	parts := make([]string, 0, len(field.spec.Options))
	for i, option := range field.spec.Options {
		if i == field.enumIndex {
			parts = append(parts, selectedActionButtonStyle.Render(option))
		} else {
			parts = append(parts, actionButtonStyle.Render(option))
		}
	}
	return strings.Join(parts, " ")
}

func (m model) renderFieldSummary() string {
	if len(m.fields) == 0 {
		return mutedStyle.Render("No fields required.")
	}
	parts := make([]string, 0, len(m.fields))
	for i, field := range m.fields {
		label := field.spec.Label
		if field.spec.Required {
			label += "*"
		}
		value := m.fieldValue(field)
		if value == "" {
			value = "-"
		}
		text := fmt.Sprintf("%s=%s", label, trimRight(value, 18))
		if i == m.fieldIdx {
			parts = append(parts, selectedFieldStyle.Render(text))
		} else {
			parts = append(parts, mutedStyle.Render(text))
		}
	}
	return trimRight(strings.Join(parts, "  "), max(1, m.width-4))
}

func (m model) fieldValue(field commandField) string {
	switch field.spec.Kind {
	case fieldText:
		return strings.TrimSpace(field.input.Value())
	case fieldEnum:
		if len(field.spec.Options) > 0 && field.enumIndex >= 0 && field.enumIndex < len(field.spec.Options) {
			return field.spec.Options[field.enumIndex]
		}
	}
	return ""
}

func (m model) renderModal() string {
	title := panelTitleStyle.Render(strings.ToUpper(m.modalTitle))
	body := m.modalVP.View()
	if strings.TrimSpace(body) == "" {
		body = mutedStyle.Render("No output.")
	}
	content := title + "\n" + body + "\n" + helpStyle.Render("Esc closes this result view.")
	return modalStyle.Width(m.modalVP.Width + 2).Height(m.modalVP.Height + 2).Render(content)
}

func (m model) renderJobLine(i int, job runSummary) string {
	width := max(1, m.jobsVP.Width)
	nameWidth := clampInt(width-12, 8, 24)
	line := fmt.Sprintf("%s %-*s %6s", jobStateToken(job.State, job.Result), nameWidth, trimRight(job.Name, nameWidth), humanStartedAt(job.StartedAt))
	if job.StderrCount > 0 {
		line += " err"
	}
	if job.State != "exited" && job.PID > 0 {
		line += " run"
	}
	if i == m.selectedIdx {
		return selectedJobStyle.Render(padRight("> "+trimRight(line, max(1, width-2)), width))
	}
	return jobStyle.Render(padRight("  "+trimRight(line, max(1, width-2)), width))
}

func (r runnerClient) list(scopeDir string) ([]runSummary, error) {
	stdout, err := r.execJSON("list", "--cwd", scopeDir)
	if err != nil {
		return nil, err
	}
	var jobs []runSummary
	if err := json.Unmarshal(stdout, &jobs); err != nil {
		return nil, fmt.Errorf("failed to parse runner list: %w", err)
	}
	return jobs, nil
}

func (r runnerClient) runAction(scopeDir string, action appAction, selected *runSummary, values map[string]string) (string, string, string, *grepFilter, error) {
	args, statusMsg, err := buildActionArgs(scopeDir, action, selected, values)
	if err != nil {
		return "", "", "", nil, err
	}
	stdout, err := r.execJSON(args...)
	if err != nil {
		return "", "", "", nil, err
	}
	runID := extractRunID(stdout)
	if action.Command == "grep" {
		filter, err := parseGrepFilter(stdout)
		if err != nil {
			return "", "", "", nil, err
		}
		return "", runID, statusMsg, filter, nil
	}
	return prettyJSON(stdout), runID, statusMsg, nil, nil
}

func buildActionArgs(scopeDir string, action appAction, selected *runSummary, values map[string]string) ([]string, string, error) {
	switch action.Command {
	case "grep":
		args := []string{"grep", "--run-id", selected.RunID, "--pattern", values["pattern"], "--stream", valueOr(values, "stream", "both"), "--cwd", scopeDir, "--pretty"}
		if values["before"] != "" {
			args = append(args, "--B", values["before"])
		}
		if values["after"] != "" {
			args = append(args, "--A", values["after"])
		}
		if values["limit"] != "" {
			args = append(args, "--limit", values["limit"])
		}
		if values["ignoreCase"] == "true" {
			args = append(args, "--ignore-case")
		}
		return args, "grep completed.", nil
	case "status":
		args := []string{"status", "--run-id", selected.RunID, "--cwd", scopeDir, "--no-wait", "--pretty"}
		if values["wait"] == "true" {
			args = []string{"status", "--run-id", selected.RunID, "--cwd", scopeDir, "--wait", "--pretty"}
		}
		if values["grep"] != "" {
			args = append(args, "--grep", values["grep"], "--grep-stream", valueOr(values, "grepStream", "both"))
		}
		return args, "status refreshed.", nil
	case "wait-for":
		args := []string{"wait-for", "--run-id", selected.RunID, "--pattern", values["pattern"], "--stream", valueOr(values, "stream", "both"), "--cwd", scopeDir, "--pretty"}
		if values["ignoreCase"] == "true" {
			args = append(args, "--ignore-case")
		}
		return args, "wait-for finished.", nil
	case "start":
		args := []string{"start", "--cmd", values["cmd"], "--cwd", scopeDir, "--pretty"}
		if values["blocking"] == "true" {
			args = append(args, "--blocking")
		} else {
			args = append(args, "--no-blocking")
		}
		if values["name"] != "" {
			args = append(args, "--name", values["name"])
		}
		if values["description"] != "" {
			args = append(args, "--description", values["description"])
		}
		return args, fmt.Sprintf("started %q", values["cmd"]), nil
	case "restart":
		return []string{"restart", "--run-id", selected.RunID, "--cwd", scopeDir}, "selected run restarted.", nil
	case "kill":
		return []string{"kill", "--run-id", selected.RunID, "--cwd", scopeDir}, "selected run killed.", nil
	case "purge":
		args := []string{"purge", "--cwd", scopeDir, "--pretty"}
		switch values["mode"] {
		case "", "terminal":
		case "success", "failed":
			args = append(args, "--result", values["mode"])
		case "older-than":
			if values["value"] == "" {
				return nil, "", fmt.Errorf("purge older-than needs a seconds value")
			}
			args = append(args, "--older-than", values["value"])
		case "run-id":
			if values["value"] == "" {
				return nil, "", fmt.Errorf("purge run-id needs a runId value")
			}
			args = append(args, "--run-id", values["value"])
		default:
			return nil, "", fmt.Errorf("unknown purge mode %q", values["mode"])
		}
		return args, "purge finished.", nil
	default:
		return nil, "", fmt.Errorf("unsupported action: %s", action.Command)
	}
}

func valueOr(values map[string]string, key, fallback string) string {
	if values[key] == "" {
		return fallback
	}
	return values[key]
}

func parseGrepFilter(raw []byte) (*grepFilter, error) {
	var response grepResponse
	if err := json.Unmarshal(raw, &response); err != nil {
		return nil, fmt.Errorf("failed to parse grep response: %w", err)
	}
	stdoutLines := []string{}
	stderrLines := []string{}
	stdoutMatches := 0
	stderrMatches := 0

	for _, match := range response.Matches {
		rendered := renderGrepMatch(match)
		switch match.Stream {
		case "stdout":
			stdoutLines = append(stdoutLines, rendered...)
			stdoutMatches++
		case "stderr":
			stderrLines = append(stderrLines, rendered...)
			stderrMatches++
		}
	}

	if len(stdoutLines) == 0 {
		stdoutLines = append(stdoutLines, fmt.Sprintf("No stdout matches for %q.", response.Pattern))
	}
	if len(stderrLines) == 0 {
		stderrLines = append(stderrLines, fmt.Sprintf("No stderr matches for %q.", response.Pattern))
	}

	return &grepFilter{
		pattern:       response.Pattern,
		stdout:        strings.Join(stdoutLines, "\n"),
		stderr:        strings.Join(stderrLines, "\n"),
		totalMatches:  response.TotalMatches,
		stdoutMatches: stdoutMatches,
		stderrMatches: stderrMatches,
		truncated:     response.Truncated,
	}, nil
}

func renderGrepMatch(match grepMatch) []string {
	lines := []string{}
	for i, line := range match.Context.Before {
		lineNo := match.LineNo - len(match.Context.Before) + i
		if lineNo < 1 {
			lineNo = 1
		}
		lines = append(lines, fmt.Sprintf("%6d- %s", lineNo, line))
	}
	lines = append(lines, fmt.Sprintf("%6d: %s", match.LineNo, match.Line))
	for i, line := range match.Context.After {
		lines = append(lines, fmt.Sprintf("%6d- %s", match.LineNo+i+1, line))
	}
	lines = append(lines, "")
	return lines
}

func (r runnerClient) execJSON(args ...string) ([]byte, error) {
	cmd := exec.Command("python3", append([]string{r.corePath}, args...)...)
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = strings.TrimSpace(stdout.String())
		}
		if msg == "" {
			msg = err.Error()
		}
		return nil, fmt.Errorf("runner %s failed: %s", strings.Join(args, " "), msg)
	}
	return stdout.Bytes(), nil
}

func findCorePath() (string, error) {
	if env := os.Getenv("RUNNER_CORE"); env != "" {
		if fileExists(env) {
			return env, nil
		}
	}

	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	exeDir := filepath.Dir(exe)
	usr, _ := user.Current()
	home := ""
	if usr != nil {
		home = usr.HomeDir
	}

	// XDG data dir (honors $XDG_DATA_HOME), where the installer places the
	// payload: <dataHome>/runner-mcp/core/runner_core.py
	dataHome := os.Getenv("XDG_DATA_HOME")
	if dataHome == "" && home != "" {
		dataHome = filepath.Join(home, ".local", "share")
	}

	candidates := []string{
		filepath.Clean(filepath.Join(exeDir, "..", "core", "runner_core.py")),
		filepath.Clean(filepath.Join(exeDir, "..", "..", "core", "runner_core.py")),
	}
	if dataHome != "" {
		candidates = append(candidates, filepath.Join(dataHome, "runner-mcp", "core", "runner_core.py"))
	}
	if cwd, err := os.Getwd(); err == nil {
		candidates = append(candidates, filepath.Join(cwd, "core", "runner_core.py"))
	}
	for _, candidate := range candidates {
		if fileExists(candidate) {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("runner_core.py not found; checked %s", strings.Join(candidates, ", "))
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func extractRunID(raw []byte) string {
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return ""
	}
	if runID, ok := payload["runId"].(string); ok {
		return runID
	}
	return ""
}

func prettyJSON(raw []byte) string {
	var out bytes.Buffer
	if err := json.Indent(&out, raw, "", "  "); err == nil {
		return out.String()
	}
	return strings.TrimSpace(string(raw))
}

func sortJobs(jobs []runSummary) []runSummary {
	sort.SliceStable(jobs, func(i, j int) bool {
		return jobs[i].StartedAt < jobs[j].StartedAt
	})
	return jobs
}

func statusBadge(state, result string) string {
	label := strings.ToUpper(state)
	style := chipStyle
	switch state {
	case "running":
		style = runningBadgeStyle
	case "starting":
		style = secondaryChipStyle
	case "exited":
		if result == "failed" {
			style = failedBadgeStyle
			label = "FAILED"
		} else {
			style = successBadgeStyle
			label = "EXITED"
		}
	}
	return style.Render(label)
}

func jobStateToken(state, result string) string {
	switch state {
	case "running":
		return "R"
	case "starting":
		return "S"
	case "exited":
		if result == "failed" {
			return "F"
		}
		return "."
	}
	return "?"
}

func actionButton(name string, selected bool) string {
	style := actionButtonStyle
	if selected {
		style = selectedActionButtonStyle
	}
	return style.Render(name)
}

func humanStartedAt(ts int64) string {
	if ts == 0 {
		return ""
	}
	return humanDuration(int(time.Now().Unix()-ts)) + " ago"
}

func humanDuration(sec int) string {
	if sec < 60 {
		return fmt.Sprintf("%ds", sec)
	}
	if sec < 3600 {
		return fmt.Sprintf("%dm", sec/60)
	}
	if sec < 86400 {
		return fmt.Sprintf("%dh", sec/3600)
	}
	return fmt.Sprintf("%dd", sec/86400)
}

func trimMiddle(s string, width int) string {
	if width <= 0 {
		return ""
	}
	if lipgloss.Width(s) <= width {
		return s
	}
	if width <= 3 {
		return strings.Repeat(".", width)
	}
	left := width/2 - 1
	right := width - left - 1
	rs := []rune(s)
	if len(rs) <= width {
		return s
	}
	return string(rs[:left]) + "…" + string(rs[len(rs)-right:])
}

func trimRight(s string, width int) string {
	if width <= 0 {
		return ""
	}
	if lipgloss.Width(s) <= width {
		return s
	}
	rs := []rune(s)
	if len(rs) <= width {
		return s
	}
	if width == 1 {
		return string(rs[:1])
	}
	return string(rs[:width-1]) + "…"
}

func padRight(s string, width int) string {
	if width <= 0 {
		return ""
	}
	current := lipgloss.Width(s)
	if current >= width {
		return trimRight(s, width)
	}
	return s + strings.Repeat(" ", width-current)
}

func panelContentWidth(totalWidth int) int {
	return max(1, totalWidth-4)
}

func panelViewportHeight(totalHeight int) int {
	return max(1, totalHeight-3)
}

func panelBlockWidth(totalWidth int) int {
	return max(1, totalWidth-2)
}

func panelBlockHeight(totalHeight int) int {
	return max(1, totalHeight-2)
}

func clampInt(v, minV, maxV int) int {
	if v < minV {
		return minV
	}
	if v > maxV {
		return maxV
	}
	return v
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

var (
	colorBg     = lipgloss.Color("#081119")
	colorPanel  = lipgloss.Color("#0F1D2B")
	colorPanel2 = lipgloss.Color("#132638")
	colorBorder = lipgloss.Color("#234159")
	colorText   = lipgloss.Color("#E7F3FF")
	colorMuted  = lipgloss.Color("#7E9AB3")
	colorTeal   = lipgloss.Color("#57D3C4")
	colorBlue   = lipgloss.Color("#63A8FF")
	colorGreen  = lipgloss.Color("#67D46E")
	colorAmber  = lipgloss.Color("#FFBF69")
	colorRed    = lipgloss.Color("#FF6B6B")
	colorPurple = lipgloss.Color("#8FB8FF")

	titleStyle = lipgloss.NewStyle().
			Foreground(colorText).
			Background(colorBlue).
			Padding(0, 1).
			Bold(true)
	boldStyle = lipgloss.NewStyle().
			Foreground(colorText).
			Bold(true)
	mutedStyle = lipgloss.NewStyle().
			Foreground(colorMuted)
	endpointStyle = lipgloss.NewStyle().
			Foreground(colorTeal)
	panelStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(colorBorder).
			Background(colorPanel).
			Padding(0, 1)
	inputPanelStyle  = panelStyle.Copy().BorderForeground(colorTeal)
	stdoutPanelStyle = panelStyle.Copy().BorderForeground(colorBlue)
	stderrPanelStyle = panelStyle.Copy().BorderForeground(colorAmber).Background(colorPanel2)
	footerPanelStyle = panelStyle.Copy().BorderForeground(colorPurple)
	modalStyle       = panelStyle.Copy().
				BorderForeground(colorTeal).
				Background(colorBg).
				Padding(1, 1)
	panelTitleStyle = lipgloss.NewStyle().
			Foreground(colorText).
			Bold(true)
	selectionStyle = lipgloss.NewStyle().
			Foreground(colorBg).
			Background(colorTeal)
	chipStyle = lipgloss.NewStyle().
			Foreground(colorBg).
			Background(colorTeal).
			Padding(0, 1)
	secondaryChipStyle = lipgloss.NewStyle().
				Foreground(colorBg).
				Background(colorBlue).
				Padding(0, 1)
	mutedToggleStyle = lipgloss.NewStyle().
				Foreground(colorMuted).
				Background(colorPanel2).
				Padding(0, 1)
	closeButtonStyle = lipgloss.NewStyle().
				Foreground(colorRed).
				Bold(true)
	successBadgeStyle = lipgloss.NewStyle().
				Foreground(colorBg).
				Background(colorGreen).
				Padding(0, 1)
	failedBadgeStyle = lipgloss.NewStyle().
				Foreground(colorBg).
				Background(colorRed).
				Padding(0, 1)
	runningBadgeStyle = lipgloss.NewStyle().
				Foreground(colorBg).
				Background(colorTeal).
				Padding(0, 1)
	promptLabelStyle = lipgloss.NewStyle().
				Foreground(colorText).
				Background(colorPurple).
				Padding(0, 1)
	actionButtonStyle = lipgloss.NewStyle().
				Foreground(colorText).
				Background(colorPanel2).
				Padding(0, 1)
	selectedActionButtonStyle = lipgloss.NewStyle().
					Foreground(colorBg).
					Background(colorTeal).
					Padding(0, 1).
					Bold(true)
	warningStyle = lipgloss.NewStyle().
			Foreground(colorAmber)
	errorStyle = lipgloss.NewStyle().
			Foreground(colorRed)
	statusMutedStyle = lipgloss.NewStyle().
				Foreground(colorMuted)
	helpStyle = lipgloss.NewStyle().
			Foreground(colorMuted)
	selectedJobStyle = lipgloss.NewStyle().
				Foreground(colorTeal).
				Bold(true)
	selectedFieldStyle = lipgloss.NewStyle().
				Foreground(colorBg).
				Background(colorAmber).
				Padding(0, 1)
	jobStyle = lipgloss.NewStyle().
			Foreground(colorText)
	dividerStyle = lipgloss.NewStyle().
			Foreground(colorBorder)
)
