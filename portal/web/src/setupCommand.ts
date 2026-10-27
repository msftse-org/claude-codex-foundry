export function setupFilename(sha256: string): string {
    if (!/^[a-f0-9]{64}$/.test(sha256))
        throw new Error("Invalid setup checksum. Download setup again.");
    return `claude-foundry-setup-${sha256}.zip`;
}

// Only a validated digest enters this fixed script. Never interpolate server URLs,
// account details, clipboard text or filenames into executable shell code.
export function terminalSetupCommand(sha256: string): string {
    const filename = setupFilename(sha256);
    const script = [
        "set -euo pipefail",
        "umask 077",
        `archive="$HOME/Downloads/${filename}"`,
        'if [ ! -f "$archive" ]; then printf "%s\\n" "ZIP not found in Downloads. Enter the ZIP full path without quotes, then press Enter:"; IFS= read -r archive; fi',
        '[ -f "$archive" ] || { printf "%s\\n" "SETUP DID NOT COMPLETE. Download setup again from your trusted organization portal." >&2; exit 1; }',
        'work="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/claude-foundry-setup.XXXXXX")"',
        'work="$(cd "$work" && /bin/pwd -P)"',
        'cleanup() { /bin/rm -rf -- "$work"; }',
        "trap cleanup EXIT",
        '/bin/cp "$archive" "$work/setup.zip"',
        'actual="$(/usr/bin/shasum -a 256 "$work/setup.zip")"',
        `[[ "\${actual%% *}" == "${sha256}" ]] || { printf "%s\\n" "SETUP DID NOT COMPLETE. The ZIP checksum does not match. Download setup again." >&2; exit 1; }`,
        'printf "%s\\n" "Setup ZIP verified. Installing organization setup tools…"',
        '/usr/bin/ditto -x -k "$work/setup.zip" "$work"',
        '/bin/bash "$work/claude-foundry/scripts/macos/install.sh"',
    ].join("; ");
    return `/bin/bash -c '${script.replaceAll("'", "'\\''")}'`;
}
