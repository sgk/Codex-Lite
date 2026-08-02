SHELL := /bin/bash

DOTNET ?= /mnt/c/Program Files/dotnet/dotnet.exe
POWERSHELL ?= /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
CONFIGURATION ?= Release
RUNTIME ?= win-x64
ARTIFACTS_DIR ?= artifacts
CURRENT_STAMP := $(shell date +%Y%m%d-%H%M%S)
RELEASE_STAMP ?= $(CURRENT_STAMP)
RELEASE_NAME ?= CodexLite-$(RELEASE_STAMP)
RELEASE_DIR := $(ARTIFACTS_DIR)/$(RELEASE_NAME)
RELEASE_ZIP := $(ARTIFACTS_DIR)/$(RELEASE_NAME).zip

.PHONY: debug-build debug-build-launch release-zip clean-release

debug-build:
	"$(POWERSHELL)" -NoProfile -ExecutionPolicy Bypass -File scripts/build-debug.ps1

debug-build-launch:
	"$(POWERSHELL)" -NoProfile -ExecutionPolicy Bypass -File scripts/build-debug.ps1

release-zip:
	mkdir -p "$(ARTIFACTS_DIR)"
	rm -rf "$(RELEASE_DIR)" "$(RELEASE_ZIP)"
	"$(DOTNET)" publish windows/CodexLite/CodexLite.csproj \
		-c "$(CONFIGURATION)" \
		-r "$(RUNTIME)" \
		--self-contained false \
		-p:PublishSingleFile=false \
		-o "$(RELEASE_DIR)"
	"$(POWERSHELL)" -NoProfile -Command \
		'Compress-Archive -Path "$(RELEASE_DIR)/*" -DestinationPath "$(RELEASE_ZIP)" -Force'
	@echo "$(RELEASE_ZIP)"

clean-release:
	rm -rf "$(ARTIFACTS_DIR)"/CodexLite-* "$(ARTIFACTS_DIR)"/CodexLite-*.zip
