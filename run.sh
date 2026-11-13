#!/usr/bin/env bash
# Launch Haunted Files. Pass a folder to choose the outermost room.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 haunted_files.py "$@"
