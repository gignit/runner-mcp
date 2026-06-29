# runner-mcp -- build / install / release
#
#   make build     Compile for THIS machine (TS -> mcp/dist, Go TUI -> bin/).
#   make install   Build, stage a local release tarball, then run install.sh
#                  against it (dogfoods the exact installer users run).
#   make release   Cross-compile all targets, bundle tarballs + SHA256SUMS,
#                  stamp install.sh with the version, and publish a GitHub
#                  Release (gh release create). This is the only path that
#                  touches GitHub.
#
# POSIX-friendly; works on macOS (BSD tools) and Linux (GNU tools).

SHELL := /bin/sh

TOOL        := runner-mcp
VERSION     := $(shell cat VERSION)
REPO        := gignit/runner-mcp

ROOT        := $(shell pwd)
MCP_DIR     := $(ROOT)/mcp
TUI_DIR     := $(ROOT)/tui
BIN_DIR     := $(ROOT)/bin
DIST_DIR    := $(ROOT)/dist

# Cross-compile matrix for `make release`.
PLATFORMS   := darwin-amd64 darwin-arm64 linux-amd64 linux-arm64

# This machine's platform (for `make install`).
HOST_OS     := $(shell uname -s | tr '[:upper:]' '[:lower:]' | sed 's/darwin/darwin/;s/linux/linux/')
HOST_ARCH   := $(shell uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/;s/arm64/arm64/')
HOST_PLATFORM := $(HOST_OS)-$(HOST_ARCH)

.PHONY: all build build-mcp build-tui clean install release stage-host help check-deps

all: build

help:
	@echo "runner-mcp targets:"
	@echo "  build     Compile for this machine (mcp/dist + bin/runner-cli)"
	@echo "  install   Build + run install.sh locally (dogfoods the installer)"
	@echo "  release   Cross-compile all platforms + publish GitHub Release"
	@echo "  clean     Remove build artifacts"

# ---------------------------------------------------------------------------
# Build (this machine)
# ---------------------------------------------------------------------------

build: build-mcp build-tui
	@echo "+ Built $(TOOL) v$(VERSION) for $(HOST_PLATFORM)"

build-mcp:
	@echo "> Building MCP server (TypeScript -> mcp/dist)"
	@cd "$(MCP_DIR)" && npm install --silent && npm run build --silent
	@echo "+ MCP built at $(MCP_DIR)/dist"

build-tui:
	@echo "> Building TUI (Go -> bin/runner-cli)"
	@mkdir -p "$(BIN_DIR)"
	@cd "$(TUI_DIR)" && go build -o "$(BIN_DIR)/runner-cli" .
	@echo "+ TUI built at $(BIN_DIR)/runner-cli"

# ---------------------------------------------------------------------------
# Payload staging -- assemble the install tree + tarball for one platform.
#   $(1) = platform (os-arch); the Go binary must already exist at
#   $(BIN_DIR)/runner-cli-$(1).
# ---------------------------------------------------------------------------

# Stage the host payload using the already-built host binary, then tarball it.
stage-host: build
	@echo "> Staging host payload for $(HOST_PLATFORM)"
	@cp "$(BIN_DIR)/runner-cli" "$(BIN_DIR)/runner-cli-$(HOST_PLATFORM)"
	@$(MAKE) --no-print-directory _stage PLATFORM=$(HOST_PLATFORM)

# Internal: assemble dist/stage-<PLATFORM>/ and tar it into dist/.
_stage:
	@test -n "$(PLATFORM)" || { echo "x _stage requires PLATFORM=os-arch"; exit 1; }
	@stage="$(DIST_DIR)/stage-$(PLATFORM)/$(TOOL)"; \
	rm -rf "$(DIST_DIR)/stage-$(PLATFORM)"; \
	mkdir -p "$$stage/core" "$$stage/lib" "$$stage/docs" "$$stage/mcp" "$$stage/bin"; \
	cp -R "$(ROOT)/core/." "$$stage/core/"; \
	cp -R "$(ROOT)/lib/."  "$$stage/lib/"; \
	cp -R "$(ROOT)/docs/." "$$stage/docs/"; \
	cp -R "$(MCP_DIR)/dist" "$$stage/mcp/dist"; \
	cp -R "$(MCP_DIR)/node_modules" "$$stage/mcp/node_modules"; \
	cp "$(MCP_DIR)/package.json" "$$stage/mcp/"; \
	cp "$(BIN_DIR)/runner-cli-$(PLATFORM)" "$$stage/bin/runner-cli"; \
	chmod +x "$$stage/bin/runner-cli" "$$stage/lib/runnerlog" 2>/dev/null || true; \
	find "$$stage" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true; \
	mkdir -p "$(DIST_DIR)"; \
	tar -czf "$(DIST_DIR)/$(TOOL)-$(VERSION)-$(PLATFORM).tar.gz" -C "$(DIST_DIR)/stage-$(PLATFORM)" "$(TOOL)"; \
	rm -rf "$(DIST_DIR)/stage-$(PLATFORM)"; \
	echo "+ Staged $(DIST_DIR)/$(TOOL)-$(VERSION)-$(PLATFORM).tar.gz"

# ---------------------------------------------------------------------------
# Install (dogfood the real installer against a locally-built artifact)
# ---------------------------------------------------------------------------

install: stage-host
	@echo "> Generating SHA256SUMS for local artifact"
	@cd "$(DIST_DIR)" && ( command -v sha256sum >/dev/null 2>&1 && sha256sum *.tar.gz > SHA256SUMS || shasum -a 256 *.tar.gz > SHA256SUMS )
	@echo "> Running install.sh against local dist (RUNNER_MCP_DIST=$(DIST_DIR))"
	@RUNNER_MCP_DIST="$(DIST_DIR)" RUNNER_MCP_VERSION="$(VERSION)" sh "$(ROOT)/install.sh"

# ---------------------------------------------------------------------------
# Release (cross-compile everything + publish to GitHub)
# ---------------------------------------------------------------------------

release: check-deps build-mcp
	@echo "> Cross-compiling TUI for: $(PLATFORMS)"
	@mkdir -p "$(BIN_DIR)" "$(DIST_DIR)"
	@rm -f "$(DIST_DIR)"/*.tar.gz "$(DIST_DIR)/SHA256SUMS" 2>/dev/null || true
	@for p in $(PLATFORMS); do \
		os="$${p%-*}"; arch="$${p#*-}"; \
		echo "  - $$p"; \
		( cd "$(TUI_DIR)" && GOOS="$$os" GOARCH="$$arch" CGO_ENABLED=0 go build -o "$(BIN_DIR)/runner-cli-$$p" . ) || exit 1; \
		$(MAKE) --no-print-directory _stage PLATFORM=$$p || exit 1; \
	done
	@echo "> Generating SHA256SUMS"
	@cd "$(DIST_DIR)" && ( command -v sha256sum >/dev/null 2>&1 && sha256sum *.tar.gz > SHA256SUMS || shasum -a 256 *.tar.gz > SHA256SUMS )
	@echo "> Stamping install.sh with version $(VERSION)"
	@sed 's/^RUNNER_MCP_VERSION="$${RUNNER_MCP_VERSION:-[0-9.]*}"/RUNNER_MCP_VERSION="$${RUNNER_MCP_VERSION:-$(VERSION)}"/' install.sh > "$(DIST_DIR)/install.sh"
	@chmod +x "$(DIST_DIR)/install.sh"
	@echo "> Publishing GitHub Release v$(VERSION)"
	@gh release view "v$(VERSION)" >/dev/null 2>&1 \
		&& gh release upload "v$(VERSION)" "$(DIST_DIR)"/*.tar.gz "$(DIST_DIR)/SHA256SUMS" "$(DIST_DIR)/install.sh" --clobber \
		|| gh release create "v$(VERSION)" "$(DIST_DIR)"/*.tar.gz "$(DIST_DIR)/SHA256SUMS" "$(DIST_DIR)/install.sh" \
			--title "$(TOOL) v$(VERSION)" --notes "Release $(VERSION). Install: curl -fsSL https://github.com/$(REPO)/releases/download/v$(VERSION)/install.sh | sh"
	@echo "+ Released v$(VERSION)"

check-deps:
	@command -v go   >/dev/null 2>&1 || { echo "x go is required for release"; exit 1; }
	@command -v node >/dev/null 2>&1 || { echo "x node is required"; exit 1; }
	@command -v gh   >/dev/null 2>&1 || { echo "x gh (GitHub CLI) is required for release"; exit 1; }

clean:
	@rm -rf "$(BIN_DIR)" "$(DIST_DIR)" "$(MCP_DIR)/dist"
	@echo "+ Cleaned build artifacts"
