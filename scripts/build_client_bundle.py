#!/usr/bin/env python3
"""Package an explicit allowlist of client files; never include local admin state."""

import argparse
import json
import zipfile
from pathlib import Path

from claude_foundry.config import load_client

ALLOWED = {
    "schema_version",
    "profile_name",
    "tenant_id",
    "gateway_url",
    "token_resource",
    "auth_mode",
    "client_id",
    "models",
    "conversation_import",
    "client_type",
    "default_model",
}
COMBINED_README = """# Claude & Codex on Azure

Install your organization's approved Claude Desktop, Claude Code and/or Codex first. Then open
`Install.command` and complete Microsoft sign-in once. Claude is configured on every Mac; Codex is
configured when Codex is installed (run `Install.command` again after installing it later).
Reopen the desktop apps after saving active work.

See [Mac installation and restore](docs/macos.md) and [Codex setup](docs/codex.md).
"""


def build(profiles, output, root):
    if isinstance(profiles, (str, Path)):
        profiles = [profiles]
    configs = [load_client(profile) for profile in profiles]
    if any(set(config) - ALLOWED for config in configs):
        raise ValueError("Use admin export-client first; the bundle accepts a client profile only")
    kinds = [config.get("client_type", "claude") for config in configs]
    if not configs or len(configs) > 2 or len(set(kinds)) != len(kinds):
        raise ValueError("Supply one Claude profile, one Codex profile, or one of each")
    if len({config["tenant_id"] for config in configs}) != 1:
        raise ValueError("Combined profiles must use the same Microsoft Entra tenant")
    by_kind = dict(zip(kinds, configs, strict=True))
    config = by_kind.get("claude") or by_kind["codex"]
    files = [
        root / p
        for p in (
            "pyproject.toml",
            "requirements-auth.lock",
            "Install.command",
            "scripts/macos/install.sh",
            "docs/macos.md",
            "docs/conversation-migration.md",
            "docs/codex.md",
            "docs/codex-validation.md",
        )
    ]
    files += sorted((root / "src/claude_foundry").glob("*.py"))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            if path.is_symlink():
                raise ValueError(f"Symlinks are not accepted: {path}")
            bundle.write(path, "claude-foundry/" + str(path.relative_to(root)))
        if len(by_kind) == 2:
            bundle.writestr("claude-foundry/README.md", COMBINED_README)
        elif config.get("client_type") == "codex":
            bundle.writestr(
                "claude-foundry/README.md",
                "# Codex on Azure\n\nInstall your organization's approved Codex Desktop or CLI, then open `Install.command` and complete Microsoft sign-in. Reopen the desktop app after saving active work.\n\nSee [Codex setup, verification, existing conversations and restore](docs/codex.md). Administrator cross-references require the full organization repository.\n",
            )
        else:
            bundle.write(root / "docs/macos.md", "claude-foundry/README.md")
        bundle.writestr("claude-foundry/client.json", json.dumps(config, indent=2) + "\n")
        if len(by_kind) == 2:
            bundle.writestr(
                "claude-foundry/codex-client.json", json.dumps(by_kind["codex"], indent=2) + "\n"
            )
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        action="append",
        required=True,
        help="Client profile; repeat once to combine a Claude and a Codex profile",
    )
    parser.add_argument("--output", type=Path, default=Path("dist/claude-foundry-macos.zip"))
    args = parser.parse_args()
    print(build(args.profile, args.output, Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    main()
