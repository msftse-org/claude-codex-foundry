#!/bin/bash
# Installs an isolated runtime. No sudo, shell-profile edits, or Azure extensions.
set -euo pipefail
umask 077
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
task_profile="$task_root/client.json"
task_install_az=false
task_login=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) task_profile="$2"; shift 2 ;;
    --install-azure-cli) task_install_az=true; shift ;;
    --no-login) task_login=false; shift ;;
    *) echo "Usage: install.sh [--config FILE] [--install-azure-cli] [--no-login]" >&2; exit 2 ;;
  esac
done
[[ "$(uname -s)" == Darwin ]] || { echo 'This installer supports macOS.' >&2; exit 1; }
[[ -f "$task_profile" ]] || { echo "Missing organization profile: $task_profile" >&2; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  task_installer="$(mktemp)"
  trap 'rm -f "$task_installer"' EXIT
  curl --proto '=https' --tlsv1.2 -fsSL https://astral.sh/uv/0.11.13/install.sh -o "$task_installer"
  UV_NO_MODIFY_PATH=1 sh "$task_installer"
fi
if [[ "$task_install_az" == true ]] && ! command -v az >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    echo 'Azure CLI installation requires Homebrew. Install Homebrew using your organization’s approved method, or request an MSAL profile.' >&2
    exit 1
  fi
  brew install --cask azure-cli
fi
task_runtime="$HOME/.local/share/claude-foundry/runtime"
mkdir -p "$(dirname "$task_runtime")"
if [[ ! -x "$task_runtime/bin/python" ]]; then
  uv venv --python 3.12 --managed-python "$task_runtime"
fi
# Dependencies are pinned with hashes; install the reviewed local project separately.
uv pip sync --python "$task_runtime/bin/python" --require-hashes "$task_root/requirements-auth.lock"
uv pip install --python "$task_runtime/bin/python" --no-deps "$task_root"
if [[ "$task_login" == true ]]; then
  "$task_runtime/bin/claude-foundry" login --config "$task_profile"
fi
"$task_runtime/bin/claude-foundry" install --config "$task_profile"
echo "Installed. Restart Claude Desktop. Recovery: $task_runtime/bin/claude-foundry restore"
