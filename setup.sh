#!/bin/bash
# First-run setup for the voice assistant. Usage: ./setup.sh [all|check|install|profile|migrate] [options]
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(uname -s)" != "Darwin" ]; then
  echo "This assistant runs only on macOS."; exit 1
fi
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]; then
  echo "This Mac is not Apple Silicon. The local speech models need an M1 or later."; exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Install the Command Line Tools first. Run: xcode-select --install"
  echo "Then run ./setup.sh again."; exit 1
fi
if [ $# -eq 0 ]; then set -- all; fi
exec /usr/bin/python3 scripts/voz_setup.py "$@"
