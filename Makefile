SHELL := /bin/bash

DOTNET ?= /mnt/c/Program Files/dotnet/dotnet.exe
POWERSHELL ?= /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
CONFIGURATION ?= Release
RUNTIME ?= win-x64
VERSION ?= 0.4.0
ARTIFACTS_DIR ?= artifacts
RELEASE_NAME ?= CodexLite-v$(VERSION)-$(RUNTIME)
RELEASE_DIR := $(ARTIFACTS_DIR)/$(RELEASE_NAME)
RELEASE_ZIP := $(ARTIFACTS_DIR)/$(RELEASE_NAME).zip

.PHONY: debug-build debug-build-launch release-zip clean-release

debug-build:
	"$(POWERSHELL)" -NoProfile -ExecutionPolicy Bypass -File scripts/build-debug.ps1

debug-build-launch:
	"$(POWERSHELL)" -NoProfile -ExecutionPolicy Bypass -File scripts/build-debug.ps1

release-zip:
	"$(POWERSHELL)" -NoProfile -ExecutionPolicy Bypass -File scripts/build-release.ps1 \
		-Version "$(VERSION)" \
		-Configuration "$(CONFIGURATION)" \
		-Runtime "$(RUNTIME)" \
		-OutputDirectory "$(RELEASE_DIR)" \
		-ZipPath "$(RELEASE_ZIP)"
	@echo "$(RELEASE_ZIP)"

clean-release:
	rm -rf "$(ARTIFACTS_DIR)"/CodexLite-* "$(ARTIFACTS_DIR)"/CodexLite-*.zip
