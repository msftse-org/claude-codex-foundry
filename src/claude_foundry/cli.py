"""CLI for organization administration and user onboarding."""

import argparse
import json
import sys
from pathlib import Path

from . import (
    __version__,
    auth,
    client,
    codex_client,
    codex_gateway,
    codex_history,
    codex_models,
    codex_verify,
    configuration_refresh,
    configuration_schedule,
    gateway,
    models,
)
from .azure import Azure, account_path
from .config import export_client, load_client, load_org, write_json
from .verify import verify


def parser():
    p = argparse.ArgumentParser(prog="claude-foundry", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    actions = p.add_subparsers(dest="action", required=True)
    token = actions.add_parser("token", help="Print a token; never starts interactive sign-in")
    token.add_argument("--config", type=Path)
    login = actions.add_parser("login", help="Open the system browser for Microsoft sign-in")
    login.add_argument("--config", type=Path)
    login.add_argument("--device-code", action="store_true")
    install = actions.add_parser(
        "install", help="Configure Claude or Codex on macOS using the profile type"
    )
    install.add_argument("--config", type=Path, required=True)
    install.add_argument("--login", action="store_true")
    install.add_argument(
        "--enable-history-import",
        action="store_true",
        help="Enable Claude's import wizard; conversations are copied only after user selection",
    )
    enroll = actions.add_parser(
        "enroll", help="Sign in and enroll macOS clients using a reviewed organization descriptor"
    )
    enroll.add_argument("--descriptor", type=Path, required=True)
    enroll.add_argument("--client", choices=["claude", "codex"], action="append", dest="clients")
    enroll.add_argument("--codex-binary", type=Path)
    enroll.add_argument(
        "--migrate-existing",
        action="store_true",
        help="Explicitly migrate matching legacy gateway profiles; rejects routing mismatches",
    )
    refresh = actions.add_parser(
        "refresh", help="Refresh enrolled model settings without signing in or replacing routing"
    )
    refresh.add_argument(
        "--login",
        action="store_true",
        help="Explicit foreground reauthentication of the bound user",
    )
    actions.add_parser(
        "enrolled-token", help="Print the enrolled user's inference token; never sign in"
    )
    actions.add_parser("configuration-status", help="Show the last successful enrolled refresh")
    actions.add_parser("enable-refresh", help="Enable the enrolled per-user background refresh job")
    actions.add_parser("disable-refresh", help="Pause automatic refresh without removing settings")
    actions.add_parser("refresh-background", help="Run one quiet noninteractive refresh attempt")
    actions.add_parser(
        "unenroll",
        help="Pause refresh and restore enrolled settings; retain encrypted credentials and history",
    )
    restore = actions.add_parser(
        "restore", help="Restore managed settings while preserving subsequent edits"
    )
    restore.add_argument("--client", choices=["claude", "codex"], default="claude")
    history = actions.add_parser(
        "export-codex-history",
        help="Export visible messages locally for a reviewed Azure handoff; preserves the source",
    )
    history.add_argument("--thread-id", required=True)
    history.add_argument("--output", type=Path, required=True)
    history.add_argument("--codex-binary", default="codex")
    check = actions.add_parser("verify", help="Make small billable gateway inference requests")
    check.add_argument("--config", type=Path)
    check.add_argument("--all-models", action="store_true")
    check.add_argument(
        "--budgeted", action="store_true", help="Verify a shared budget-runtime Claude route"
    )
    check.add_argument(
        "--governed", action="store_true", help="Verify published Claude portal governance"
    )
    admin = actions.add_parser(
        "admin", help="Organization administrator commands; requires Azure CLI"
    )
    admin.add_argument("--config", type=Path, required=True)
    commands = admin.add_subparsers(dest="command", required=True)
    init_codex = commands.add_parser(
        "init-codex", help="Create a separate Codex API profile using this foundation"
    )
    init_codex.add_argument("--output", type=Path, required=True)
    qualify = commands.add_parser(
        "qualify", help="Test Responses streaming, tools and replay; billable"
    )
    qualify.add_argument("--report", type=Path, default=Path(".local/codex-qualification.json"))
    qualify.add_argument(
        "--publish-passed",
        action="store_true",
        help="Save only passing models to this organization profile",
    )
    plan = commands.add_parser(
        "models", help="Discover quota, then optionally deploy missing models"
    )
    plan.add_argument("--capacity", type=int, default=10)
    plan.add_argument(
        "--sku",
        choices=["GlobalStandard", "DataZoneStandard", "Standard"],
        help="Restrict new deployments to this SKU; Claude defaults to GlobalStandard, Codex prefers available metered SKUs",
    )
    plan.add_argument(
        "--model",
        action="append",
        dest="model_names",
        help="Limit discovery/deployment to this catalog model name; repeat for several models",
    )
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
            auth.helper(args.config)
        elif args.action == "login":
            auth.login(args.config, args.device_code)
        elif args.action == "install":
            if sys.platform != "darwin":
                raise RuntimeError("The settings installer currently supports macOS only")
            c = load_client(args.config)
            if args.enable_history_import:
                c.setdefault("conversation_import", {})["enabled"] = True
            installer = codex_client if c.get("client_type") == "codex" else client
            installer.check_managed_preferences()
            # Validate credentials before replacing a working configuration.
            if args.login:
                auth.login(args.config)
            auth.credential(c)
            print(json.dumps(installer.install(c), indent=2))
            print(
                "Reopen the desktop app to apply the profile. Existing conversations are preserved."
            )
            if c.get("conversation_import", {}).get("enabled"):
                print(
                    "Bring existing work: Settings → Import & export → Import… in Claude Desktop."
                )
                print("Review the source and import summary. Original conversations stay in place.")
        elif args.action in {
            "enroll",
            "refresh",
            "enrolled-token",
            "configuration-status",
            "enable-refresh",
            "disable-refresh",
            "refresh-background",
            "unenroll",
        }:
            if sys.platform != "darwin":
                raise RuntimeError("Organization enrollment currently supports macOS only")
            if args.action == "enroll":
                result = configuration_refresh.enroll(
                    args.descriptor,
                    clients=args.clients,
                    codex_binary=args.codex_binary,
                    migrate_existing=args.migrate_existing,
                )
            elif args.action == "refresh":
                result = configuration_refresh.refresh(login=args.login)
            elif args.action == "enrolled-token":
                print(configuration_refresh.inference_token())
                return 0
            elif args.action == "refresh-background":
                return configuration_schedule.run()
            elif args.action == "unenroll":
                result = configuration_refresh.restore()
            elif args.action in {"enable-refresh", "disable-refresh"}:
                result = configuration_schedule.configure(enabled=args.action == "enable-refresh")
            else:
                result = configuration_refresh.status()
            print(json.dumps(result, indent=2))
            if args.action in {"enroll", "refresh"}:
                print(
                    "Model settings updated. Reopen clients if their model picker is already open."
                )
        elif args.action == "restore":
            if (Path.home() / configuration_refresh.STATE / "enrollment.json").exists():
                raise RuntimeError("Use unenroll to restore an enrolled installation.")
            if args.client == "codex":
                print(codex_client.restore())
            else:
                client.restore()
        elif args.action == "export-codex-history":
            print(
                json.dumps(
                    codex_history.export(args.thread_id, args.output, args.codex_binary), indent=2
                )
            )
        elif args.action == "verify":
            c = load_client(args.config) if args.config else auth.installed_config()
            if c.get("client_type") == "codex":
                if args.governed or args.budgeted:
                    raise ValueError("--governed and --budgeted apply only to Claude gateways")
                selected = (
                    c["models"]
                    if args.all_models
                    else [next(m for m in c["models"] if m["name"] == c["default_model"])]
                )
                report = [codex_verify.qualify(c, m) for m in selected]
                print(json.dumps(report, indent=2))
                return 0 if all(r["passed"] for r in report) else 2
            verify(
                c,
                args.all_models,
                governed=args.governed,
                **({"budgeted": True} if args.budgeted else {}),
            )
        elif args.action == "admin":
            c = load_org(args.config)
            azure = Azure(c["subscription_id"])
            is_codex = c.get("client_type") == "codex"
            if args.command == "init-codex":
                if args.output.exists():
                    raise ValueError(
                        "Output already exists; refusing to replace an organization profile"
                    )
                account = azure.request("GET", account_path(c), version=models.CS_VERSION)
                endpoint = (
                    account.get("properties", {})
                    .get("endpoints", {})
                    .get("OpenAI Language Model Instance API")
                )
                if not endpoint:
                    raise ValueError(
                        "The resource does not advertise an OpenAI endpoint; review its Foundry configuration"
                    )
                write_json(args.output, codex_models.initialize(c, endpoint))
                print(f"Wrote {args.output}")
            elif args.command == "qualify":
                if not is_codex:
                    raise ValueError("Qualification requires a Codex organization profile")
                report = []
                token = auth.credential(c)
                for m in c["models"]:
                    result = codex_verify.qualify(c, m, token)
                    report.append(result)
                    write_json(args.report, report)
                    print(
                        f"{m['name']}: {'passed' if result['passed'] else result.get('error', 'failed')}",
                        flush=True,
                    )
                if args.publish_passed:
                    passed = {r["name"] for r in report if r["passed"]}
                    if not passed:
                        raise ValueError(
                            "No model passed qualification; organization profile was preserved"
                        )
                    c["models"] = [
                        dict(m, qualified=True) for m in c["models"] if m["name"] in passed
                    ]
                    if c["default_model"] not in passed:
                        c["default_model"] = c["models"][0]["name"]
                    write_json(args.config, c)
                return 0 if all(r["passed"] for r in report) else 2
            elif args.command == "models":
                if args.capacity < 1 or args.wait_seconds < 0:
                    raise ValueError("capacity must be positive; wait-seconds must be nonnegative")
                if not is_codex and args.sku == "Standard":
                    raise ValueError(
                        "Claude supports GlobalStandard or DataZoneStandard in this planner"
                    )
                plan = (
                    codex_models.discover(
                        c, azure, args.capacity, model_names=args.model_names, sku_name=args.sku
                    )
                    if is_codex
                    else models.discover(
                        c,
                        azure,
                        args.capacity,
                        sku_name=args.sku or "GlobalStandard",
                        model_names=args.model_names,
                    )
                )
                if args.apply:
                    if not is_codex and not args.accept_marketplace_terms:
                        raise ValueError(
                            "Model deployment may accept paid Marketplace terms. Review docs/administrator.md, then pass --accept-marketplace-terms."
                        )
                    report = models.apply(c, azure, plan, args.report, args.wait_seconds)
                    if any(
                        r.get("state") in {"Failed", "Unknown", "Creating", "Updating", "Accepted"}
                        for r in report
                    ):
                        return 2
                else:
                    write_json(args.report, plan)
                    print(json.dumps(plan, indent=2))
            elif args.command == "sync-models":
                write_json(
                    args.config, (codex_models if is_codex else models).sync_models(c, azure)
                )
                print(f"Saved {len(c['models'])} succeeded deployments")
            elif args.command == "gateway":
                print(
                    json.dumps((codex_gateway if is_codex else gateway).deploy(c, azure), indent=2)
                )
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
