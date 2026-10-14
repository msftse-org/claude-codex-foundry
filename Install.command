#!/bin/bash
set -euo pipefail
task_root="$(cd "$(dirname "$0")" && pwd)"
exec "$task_root/scripts/macos/install.sh" "$@"
