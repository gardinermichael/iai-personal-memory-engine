#!/usr/bin/env bash
# Package-manager-safe post-install guidance for Homebrew.
# This intentionally delegates to install.sh's package-manager mode instead of
# the clone-oriented bootstrap path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/install.sh" --package-manager "$@"
