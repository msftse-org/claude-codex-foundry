"""CLI for organization administration and user onboarding."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__, auth, client, gateway, models
from .azure import Azure
from .config import export_client, load_client, load_org, write_json
from .verify import verify


def parser():
    p = argparse.ArgumentParser(prog="claude-foundry", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    actions = p.add_subparsers(dest="action", required=True)
    actions.add_parser("token", help="Print a token for Claude; never starts interactive sign-in")
    login = actions.add_parser("login", help="Open the system browser for Microsoft sign-in")
    login.add_argument("--config", type=Path)
    login.add_argument("--device-code", action="store_true")
    install = actions.add_parser("install", help="Configure Claude Desktop and Code on macOS")
    install.add_argument("--config", type=Path, required=True)
    install.add_argument("--login", action="store_true")
    actions.add_parser("restore", help="Restore managed settings while preserving subsequent edits")
    check = actions.add_parser("verify", help="Make small billable gateway inference requests")
    check.add_argument("--config", type=Path)
    check.add_argument("--all-models", action="store_true")
    admin = actions.add_parser(
        "admin", help="Organization administrator commands; requires Azure CLI"
    )
    admin.add_argument("--config", type=Path, required=True)
    commands = admin.add_subparsers(dest="command", required=True)
    plan = commands.add_parser(
        "models", help="Discover quota, then optionally deploy missing models"
    )
    plan.add_argument("--capacity", type=int, default=10)
    plan.add_argument("--apply", action="store_true")
    plan.add_argument("--accept-marketplace-terms", action="store_true")
    plan.add_argument("--wait-seconds", type=int, default=600)
    plan.add_argument("--report", type=Path, default=Path(".local/models-report.json"))
    commands.add_parser(
        "sync-models", help="Save succeeded Foundry deployments to organization config"
    )
    commands.add_parser("gateway", help="Deploy the dedicated APIM API and policies")
    observability = commands.add_parser(
        "observability",
        help="Enable metrics and traces for this API using the connected Application Insights logger",
    )
    observability.add_argument(
        "--enable-dimensions",
        action="store_true",
        help="Enable custom metric dimensions on the connected Application Insights component (additional monitoring charges may apply)",
    )
    export = commands.add_parser(
        "export-client", help="Write a shareable profile with no secrets or admin IDs"
    )
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--auth-mode", choices=["azure-cli", "msal"], default="azure-cli")
    export.add_argument("--client-id")
    return p


def main():
    args = parser().parse_args()
    try:
        if args.action == "token":
            auth.helper()
        elif args.action == "login":
            auth.login(args.config, args.device_code)
        elif args.action == "install":
            if sys.platform != "darwin":
                raise RuntimeError("The settings installer currently supports macOS only")
            c = load_client(args.config)
            client.check_managed_preferences()
            # Validate credentials before replacing a working configuration.
            if args.login:
                auth.login(args.config)
            auth.credential(c)
            print(json.dumps(client.install(c), indent=2))
            print("Restart Claude Desktop to apply the profile.")
        elif args.action == "restore":
            client.restore()
        elif args.action == "verify":
            verify(
                load_client(args.config) if args.config else auth.installed_config(),
                args.all_models,
            )
        elif args.action == "admin":
            c = load_org(args.config)
            azure = Azure(c["subscription_id"])
            if args.command == "models":
                if args.capacity < 1 or args.wait_seconds < 0:
                    raise ValueError("capacity must be positive; wait-seconds must be nonnegative")
                plan = models.discover(c, azure, args.capacity)
                if args.apply:
                    if not args.accept_marketplace_terms:
                        raise ValueError(
                            "Model deployment may accept paid Marketplace terms. Review docs/administrator.md, then pass --accept-marketplace-terms."
                        )
                    report = models.apply(c, azure, plan, args.report, args.wait_seconds)
                    if any(
                        r.get("state") in {"Failed", "Creating", "Updating", "Accepted"}
                        for r in report
                    ):
                        return 2
                else:
                    write_json(args.report, plan)
                    print(json.dumps(plan, indent=2))
            elif args.command == "sync-models":
                write_json(args.config, models.sync_models(c, azure))
                print(f"Saved {len(c['models'])} succeeded deployments")
            elif args.command == "gateway":
                print(json.dumps(gateway.deploy(c, azure), indent=2))
            elif args.command == "observability":
                print(
                    json.dumps(
                        gateway.configure_observability(c, azure, args.enable_dimensions), indent=2
                    )
                )
            elif args.command == "export-client":
                write_json(args.output, export_client(c, args.auth_mode, args.client_id))
                print(f"Wrote {args.output}")
        return 0
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
