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
task_reopen=""
task_defaults=""
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
      if ! "$task_runtime/bin/claude-foundry" login --config "$task_profile"; then
        task_auth="$("$task_runtime/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("auth_mode", "azure-cli"))' "$task_profile")"
        echo >&2
        echo "SETUP DID NOT COMPLETE. $task_client was not configured, so restarting it will not activate this organization." >&2
        if [[ "$task_auth" == msal ]]; then
          task_tenant="$("$task_runtime/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$task_profile")"
          task_client_id="$("$task_runtime/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["client_id"])' "$task_profile")"
          echo "If Microsoft displayed 'Need admin approval', a tenant administrator must grant consent:" >&2
          echo "https://login.microsoftonline.com/$task_tenant/adminconsent?client_id=$task_client_id" >&2
        fi
        echo "After sign-in is approved, run Install.command again. Restart the app only after this installer prints 'SETUP COMPLETE'." >&2
        exit 1
      fi
      task_signed_in="$task_signed_in$task_identity|"
    fi
  fi
  task_install_args=(install --config "$task_profile")
  if [[ "$task_history" == true && "$task_client" == claude ]]; then
    task_install_args+=(--enable-history-import)
  fi
  if ! "$task_runtime/bin/claude-foundry" "${task_install_args[@]}"; then
    echo >&2
    echo "SETUP DID NOT COMPLETE. $task_client was not configured; do not restart it yet." >&2
    echo "Resolve the sign-in or managed-settings error above, then run Install.command again." >&2
    exit 1
  fi
  task_gateway="$("$task_runtime/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway_url"])' "$task_profile")"
  task_default="$("$task_runtime/bin/python" -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c.get("default_model") or c["models"][0]["name"])' "$task_profile")"
  echo "Configured $task_client. Gateway: $task_gateway"
  task_restore="$task_restore $task_runtime/bin/claude-foundry restore --client $task_client;"
  task_reopen="${task_reopen}${task_reopen:+ and }$task_client"
  task_defaults="${task_defaults}${task_defaults:+; }$task_client: $task_default"
done
echo
echo "SETUP COMPLETE. User actions:"
echo "1. Save active work and fully quit $task_reopen."
echo "2. Reopen the app. Existing conversations and unrelated settings are preserved."
echo "3. Start a new local chat and select the approved model ($task_defaults)."
echo "4. Send a short test message. If sign-in is requested, complete it with your Microsoft work account."
echo "Recovery:${task_restore%;}"
