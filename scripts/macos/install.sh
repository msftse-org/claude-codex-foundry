#!/bin/bash
# Installs an isolated runtime. No sudo, shell-profile edits, or Azure extensions.
set -euo pipefail
umask 077
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
task_profiles=()
task_install_az=false
task_login=true
task_history=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) task_profiles+=("$2"); shift 2 ;;
    --install-azure-cli) task_install_az=true; shift ;;
    --no-login) task_login=false; shift ;;
    --enable-history-import) task_history=true; shift ;;
    *) echo "Usage: install.sh [--config FILE]... [--install-azure-cli] [--no-login] [--enable-history-import]" >&2; exit 2 ;;
  esac
done
# A combined bundle also carries codex-client.json. Codex is configured only where it is installed.
task_optional_codex=""
if [[ ${#task_profiles[@]} -eq 0 ]]; then
  task_profiles=("$task_root/client.json")
  if [[ -f "$task_root/codex-client.json" ]]; then
    task_optional_codex="$task_root/codex-client.json"
    task_profiles+=("$task_optional_codex")
  fi
fi
[[ "$(uname -s)" == Darwin ]] || { echo 'This installer supports macOS.' >&2; exit 1; }
for task_profile in "${task_profiles[@]}"; do
  [[ -f "$task_profile" ]] || { echo "Missing organization profile: $task_profile" >&2; exit 1; }
done
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
task_codex_app="${CLAUDE_FOUNDRY_CODEX_APP:-/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex}"
task_codex_cli="${CLAUDE_FOUNDRY_CODEX_CLI:-codex}"
task_signed_in="|"
task_restore=""
for task_profile in "${task_profiles[@]}"; do
  task_client="$("$task_runtime/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("client_type", "claude"))' "$task_profile")"
  if [[ "$task_profile" == "$task_optional_codex" && ! -e "$task_codex_app" ]] && ! command -v "$task_codex_cli" >/dev/null 2>&1; then
    echo "Codex is not installed on this Mac, so only Claude was configured. Install Codex, then open Install.command again." >&2
    continue
  fi
  if [[ "$task_login" == true ]]; then
    # One sign-in per identity: Claude and Codex profiles normally share it.
    task_identity="$("$task_runtime/bin/python" -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c.get("auth_mode", "azure-cli"), c["tenant_id"], c.get("client_id", ""))' "$task_profile")"
    if [[ "$task_signed_in" != *"|$task_identity|"* ]]; then
      "$task_runtime/bin/claude-foundry" login --config "$task_profile"
      task_signed_in="$task_signed_in$task_identity|"
    fi
  fi
  task_install_args=(install --config "$task_profile")
  if [[ "$task_history" == true && "$task_client" == claude ]]; then
    task_install_args+=(--enable-history-import)
  fi
  "$task_runtime/bin/claude-foundry" "${task_install_args[@]}"
  task_gateway="$("$task_runtime/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway_url"])' "$task_profile")"
  echo "Configured $task_client. Gateway: $task_gateway"
  task_restore="$task_restore $task_runtime/bin/claude-foundry restore --client $task_client;"
done
echo "Installed. Reopen the desktop app. Recovery:${task_restore%;}"
