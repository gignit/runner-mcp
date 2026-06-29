package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/charmbracelet/bubbles/viewport"
	"github.com/charmbracelet/lipgloss"
)

func TestSortJobsOldestFirst(t *testing.T) {
	jobs := []runSummary{
		{RunID: "newest", StartedAt: 300},
		{RunID: "oldest", StartedAt: 100},
		{RunID: "middle", StartedAt: 200},
	}

	got := sortJobs(jobs)
	want := []string{"oldest", "middle", "newest"}
	for i, id := range want {
		if got[i].RunID != id {
			t.Fatalf("job %d = %q, want %q", i, got[i].RunID, id)
		}
	}
}

func TestRenderJobLineFitsViewportWidth(t *testing.T) {
	m := model{
		jobsVP:      viewport.New(30, 10),
		selectedIdx: 0,
	}
	job := runSummary{
		Name:        "very-long-runner-job-name-that-must-not-overflow",
		State:       "exited",
		Result:      "failed",
		StartedAt:   100,
		StderrCount: 12,
	}

	line := m.renderJobLine(0, job)
	if got := lipgloss.Width(line); got != m.jobsVP.Width {
		t.Fatalf("selected row width = %d, want %d; row=%q", got, m.jobsVP.Width, line)
	}
}

func TestNormalizeLogForViewportNeutralizesCarriageReturns(t *testing.T) {
	raw := "start\rprogress 1\rprogress 2\nnext\r\nline\b\x00"
	got := normalizeLogForViewport(raw)

	if strings.ContainsAny(got, "\r\b\x00") {
		t.Fatalf("normalized log still contains terminal controls: %q", got)
	}
	if !strings.Contains(got, "progress 1\nprogress 2") {
		t.Fatalf("carriage-return progress was not made line-safe: %q", got)
	}
}

func TestSelectNewActiveRunIgnoresKnownActiveRuns(t *testing.T) {
	m := model{
		jobs: []runSummary{
			{RunID: "old", Name: "old", State: "exited"},
			{RunID: "current", Name: "current", State: "exited"},
			{RunID: "known-active", Name: "known-active", State: "running"},
		},
		selectedIdx: 1,
		selectedID:  "current",
		knownRunIDs: map[string]struct{}{"old": {}, "current": {}, "known-active": {}},
		runIDsReady: true,
	}

	if changed := m.selectNewActiveRun(); changed {
		t.Fatal("selectNewActiveRun jumped to a known active run")
	}
	if m.selectedID != "current" {
		t.Fatalf("selectedID = %q, want current", m.selectedID)
	}
}

func TestSelectNewActiveRunJumpsToNewActiveRun(t *testing.T) {
	m := model{
		jobs: []runSummary{
			{RunID: "old", Name: "old", State: "exited", StartedAt: 100},
			{RunID: "current", Name: "current", State: "exited", StartedAt: 200},
			{RunID: "next", Name: "next", State: "running", StartedAt: 300},
		},
		selectedIdx: 1,
		selectedID:  "current",
		knownRunIDs: map[string]struct{}{"old": {}, "current": {}},
		runIDsReady: true,
	}

	if changed := m.selectNewActiveRun(); !changed {
		t.Fatal("selectNewActiveRun did not report selecting a new active run")
	}
	if m.selectedID != "next" {
		t.Fatalf("selectedID = %q, want next", m.selectedID)
	}
}

func TestSelectNewActiveRunDoesNotJumpOnInitialLoad(t *testing.T) {
	m := model{
		jobs: []runSummary{
			{RunID: "already-running", Name: "already-running", State: "running"},
		},
		selectedIdx: 0,
		selectedID:  "already-running",
	}

	if changed := m.selectNewActiveRun(); changed {
		t.Fatal("selectNewActiveRun jumped before the initial run list was established")
	}
}

func TestOutputDividerHitIncludesAdjacentBorders(t *testing.T) {
	m := newModel("/tmp/project", "/tmp/core")
	m.width = 120
	m.height = 40
	m.layout()

	y := m.dividerRect.y + 1
	for _, x := range []int{m.dividerRect.x - 1, m.dividerRect.x, m.dividerRect.x + 1} {
		if !m.outputDividerHit(x, y) {
			t.Fatalf("outputDividerHit(%d, %d) = false, want true", x, y)
		}
	}
	if m.outputDividerHit(m.dividerRect.x-2, y) {
		t.Fatalf("outputDividerHit matched too far left of divider")
	}
	if m.outputDividerHit(m.dividerRect.x+2, y) {
		t.Fatalf("outputDividerHit matched too far right of divider")
	}
}

func TestResizeOutputSplitFromXClampsAndRelayouts(t *testing.T) {
	m := newModel("/tmp/project", "/tmp/core")
	m.width = 120
	m.height = 40
	m.layout()

	mainX := m.jobsRect.x + m.jobsRect.w + 1
	m.resizeOutputSplitFromX(mainX)
	if m.outputSplit != 0.25 {
		t.Fatalf("outputSplit = %f, want 0.25", m.outputSplit)
	}

	m.resizeOutputSplitFromX(m.width)
	if m.outputSplit != 0.75 {
		t.Fatalf("outputSplit = %f, want 0.75", m.outputSplit)
	}
	if m.dividerRect.x <= m.stdoutRect.x {
		t.Fatalf("dividerRect was not relaid out after resize: stdout=%+v divider=%+v", m.stdoutRect, m.dividerRect)
	}
}

func TestBuildGrepActionArgsFromFields(t *testing.T) {
	action := buildActions()[0]
	selected := &runSummary{RunID: "run-1"}
	values := map[string]string{
		"pattern":    "panic|FAIL",
		"stream":     "stderr",
		"before":     "2",
		"after":      "3",
		"limit":      "25",
		"ignoreCase": "true",
	}

	got, _, err := buildActionArgs("/tmp/project", action, selected, values)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		"grep", "--run-id", "run-1", "--pattern", "panic|FAIL",
		"--stream", "stderr", "--cwd", "/tmp/project", "--pretty",
		"--B", "2", "--A", "3", "--limit", "25", "--ignore-case",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("args = %#v, want %#v", got, want)
	}
}

func TestParseGrepFilterSplitsStreams(t *testing.T) {
	raw := []byte(`{
		"pattern":"FAIL",
		"totalMatches":2,
		"truncated":false,
		"matches":[
			{"stream":"stdout","lineNo":12,"line":"FAIL stdout","context":{"before":["before"],"after":["after"]}},
			{"stream":"stderr","lineNo":3,"line":"FAIL stderr"}
		]
	}`)

	filter, err := parseGrepFilter(raw)
	if err != nil {
		t.Fatal(err)
	}
	if filter.stdoutMatches != 1 || filter.stderrMatches != 1 {
		t.Fatalf("matches stdout/stderr = %d/%d, want 1/1", filter.stdoutMatches, filter.stderrMatches)
	}
	if !strings.Contains(filter.stdout, "    12: FAIL stdout") {
		t.Fatalf("stdout filter missing formatted match: %q", filter.stdout)
	}
	if !strings.Contains(filter.stderr, "     3: FAIL stderr") {
		t.Fatalf("stderr filter missing formatted match: %q", filter.stderr)
	}
}

func TestLayoutPlacesInputColumnLeftOfStdout(t *testing.T) {
	m := newModel("/tmp/project", "/tmp/core")
	m.width = 140
	m.height = 40
	m.inputVisible = true
	m.layout()

	if m.inputRect.w <= 0 {
		t.Fatalf("input panel not laid out: %+v", m.inputRect)
	}
	if m.inputRect.x >= m.stdoutRect.x {
		t.Fatalf("input column not left of stdout: input=%+v stdout=%+v", m.inputRect, m.stdoutRect)
	}
	if m.stdoutRect.x >= m.stderrRect.x {
		t.Fatalf("stdout not left of stderr: stdout=%+v stderr=%+v", m.stdoutRect, m.stderrRect)
	}
	// Input column begins just right of the jobs panel.
	wantInputX := m.jobsRect.x + m.jobsRect.w + 1
	if m.inputRect.x != wantInputX {
		t.Fatalf("input x = %d, want %d", m.inputRect.x, wantInputX)
	}
}

func TestLayoutInputOnlyTakesFullMainWidth(t *testing.T) {
	m := newModel("/tmp/project", "/tmp/core")
	m.width = 140
	m.height = 40
	m.inputVisible = true
	m.stdoutVisible = false
	m.stderrVisible = false
	m.layout()

	mainX := m.jobsRect.x + m.jobsRect.w + 1
	wantW := m.width - mainX
	if m.inputRect.x != mainX || m.inputRect.w != wantW {
		t.Fatalf("input rect = %+v, want x=%d w=%d", m.inputRect, mainX, wantW)
	}
	if m.stdoutRect.w != 0 || m.stderrRect.w != 0 {
		t.Fatalf("hidden panels should have zero rects: stdout=%+v stderr=%+v", m.stdoutRect, m.stderrRect)
	}
}

func TestTogglePanelInputFlipsVisibility(t *testing.T) {
	m := newModel("/tmp/project", "/tmp/core")
	m.width = 140
	m.height = 40
	m.layout()

	if m.inputVisible {
		t.Fatal("input panel should start hidden")
	}
	m.togglePanel("Input")
	if !m.inputVisible {
		t.Fatal("togglePanel did not show input")
	}
	if m.inputRect.w <= 0 {
		t.Fatalf("input rect not laid out after toggle: %+v", m.inputRect)
	}
	m.togglePanel("Input")
	if m.inputVisible {
		t.Fatal("togglePanel did not hide input")
	}
	if m.inputRect != (rect{}) {
		t.Fatalf("hidden input rect should be zero: %+v", m.inputRect)
	}
}

func TestTogglePanelAllowsAllMainPanelsClosed(t *testing.T) {
	m := newModel("/tmp/project", "/tmp/core")
	m.width = 140
	m.height = 40
	m.layout()

	m.togglePanel("Stdout")
	m.togglePanel("Stderr")
	if m.stdoutVisible || m.stderrVisible {
		t.Fatal("stdout/stderr should both be closeable")
	}
	if m.inputVisible {
		t.Fatal("input was never opened")
	}
}

func TestHeaderButtonsHitTestToggle(t *testing.T) {
	m := newModel("/tmp/project", "/tmp/core")
	m.width = 140
	m.height = 40
	m.layout()

	var inputBtn *headerButton
	for i := range m.headerButtons {
		if m.headerButtons[i].name == "Input" {
			inputBtn = &m.headerButtons[i]
		}
	}
	if inputBtn == nil {
		t.Fatal("Input header button not found")
	}
	if !inputBtn.rect.contains(inputBtn.rect.x, inputBtn.rect.y) {
		t.Fatalf("Input button rect does not contain its own origin: %+v", inputBtn.rect)
	}
	// Buttons live on line 1 (the title row), right-justified to the top-right
	// corner.
	if inputBtn.rect.y != 1 {
		t.Fatalf("header button bar should be on row 1 (top), got %d", inputBtn.rect.y)
	}
	// Right-justified: the last button (Stderr) should end near the right edge
	// (within the 1-col right padding).
	var lastBtn *headerButton
	for i := range m.headerButtons {
		if m.headerButtons[i].name == "Stderr" {
			lastBtn = &m.headerButtons[i]
		}
	}
	if lastBtn == nil {
		t.Fatal("Stderr header button not found")
	}
	if end := lastBtn.rect.x + lastBtn.rect.w; end < m.width-3 || end > m.width {
		t.Fatalf("buttons not right-justified: last button ends at %d, width %d", end, m.width)
	}
}

func TestCloseButtonRectInsidePanelTitleRow(t *testing.T) {
	m := newModel("/tmp/project", "/tmp/core")
	m.width = 140
	m.height = 40
	m.layout()

	cr := m.stdoutCloseRect
	if cr.w != 3 {
		t.Fatalf("close button width = %d, want 3", cr.w)
	}
	// Title row is one below the panel's top border.
	if cr.y != m.stdoutRect.y+1 {
		t.Fatalf("close button y = %d, want %d", cr.y, m.stdoutRect.y+1)
	}
	// The [x] must fall within the stdout panel's horizontal span.
	if cr.x < m.stdoutRect.x || cr.x+cr.w > m.stdoutRect.x+m.stdoutRect.w {
		t.Fatalf("close button x=%d w=%d outside stdout panel %+v", cr.x, cr.w, m.stdoutRect)
	}
}

func TestReadInputContentNormalRunUsesCmd(t *testing.T) {
	dir := t.TempDir()
	meta := `{"runId":"r1","cmd":"go test ./...","cwd":"/tmp"}`
	if err := os.WriteFile(filepath.Join(dir, "meta.json"), []byte(meta), 0o644); err != nil {
		t.Fatal(err)
	}
	got := readInputContent(dir)
	if got != "go test ./..." {
		t.Fatalf("readInputContent = %q, want %q", got, "go test ./...")
	}
}

func TestReadInputContentSubAgentPrefersPromptMd(t *testing.T) {
	dir := t.TempDir()
	meta := `{"runId":"r1","cmd":"opencode run ...","agentRuntime":"opencode"}`
	if err := os.WriteFile(filepath.Join(dir, "meta.json"), []byte(meta), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "prompt.md"), []byte("investigate the bug\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got := readInputContent(dir)
	if got != "investigate the bug" {
		t.Fatalf("readInputContent = %q, want sub-agent prompt", got)
	}
}

func TestReadInputContentSubAgentFallsBackToCmd(t *testing.T) {
	dir := t.TempDir()
	meta := `{"runId":"r1","cmd":"opencode run ...","agentRuntime":"opencode"}`
	if err := os.WriteFile(filepath.Join(dir, "meta.json"), []byte(meta), 0o644); err != nil {
		t.Fatal(err)
	}
	got := readInputContent(dir)
	if got != "opencode run ..." {
		t.Fatalf("readInputContent = %q, want meta cmd fallback", got)
	}
}
