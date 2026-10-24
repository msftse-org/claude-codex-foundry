#!/usr/bin/env python3
"""Grant or remove developer access to Claude & Codex on Azure.

Adds people to the deployment's Microsoft Entra developer group, which holds the Foundry
inference role. Accepts work emails, user principal names or object IDs, on the command
line or in a CSV whose first column lists them. Also writes a CSV for the portal's member
import, used when portal governance is published.
"""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from claude_foundry.config import read_json

HEADERS = {"email", "upn", "user", "object_id", "id"}


def az(*args):
    result = subprocess.run(
        ["az", *args, "-o", "json", "--only-show-errors"], text=True, capture_output=True
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Azure CLI command failed")
    return json.loads(result.stdout) if result.stdout.strip() else None


def is_object_id(value):
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def resolve(identity, run=az):
    try:
        user = run("ad", "user", "show", "--id", identity)
    except RuntimeError:
        if is_object_id(identity):
            raise LookupError(f"No Entra user has object ID {identity}") from None
        # Guests sign in with their home email, which differs from their tenant UPN.
        escaped = identity.replace("'", "''")
        users = run("ad", "user", "list", "--filter", f"mail eq '{escaped}'")
        if len(users) != 1:
            raise LookupError(f"No unique Entra user matches {identity}") from None
        user = users[0]
    return {
        "object_id": user["id"],
        "name": user.get("displayName") or identity,
        "email": user.get("mail") or user.get("userPrincipalName") or identity,
    }


def identities(values, csv_path=None):
    found = list(values)
    if csv_path:
        with open(csv_path, newline="", encoding="utf-8-sig") as stream:
            for row in csv.reader(stream):
                if row and row[0].strip() and row[0].strip().lower() not in HEADERS:
                    found.append(row[0].strip())
    return list(dict.fromkeys(v.strip() for v in found if v.strip()))


def manage(people, group, remove=False, run=az):
    results = []
    for identity in people:
        try:
            user = resolve(identity, run)
        except LookupError as error:
            results.append({"identity": identity, "action": "not found", "error": str(error)})
            continue
        oid = user["object_id"]
        member = run("ad", "group", "member", "check", "--group", group, "--member-id", oid)[
            "value"
        ]
        if remove:
            action = "removed" if member else "not a member"
            if member:
                run("ad", "group", "member", "remove", "--group", group, "--member-id", oid)
        else:
            action = "already has access" if member else "added"
            if not member:
                run("ad", "group", "member", "add", "--group", group, "--member-id", oid)
        results.append({"identity": identity, "action": action, **user})
    return results


def write_portal_csv(path, results):
    rows = [r for r in results if "object_id" in r]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["object_id", "name", "email"])
        writer.writerows([r["object_id"], r["name"], r["email"]] for r in rows)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("people", nargs="*", help="Work emails, UPNs or Entra object IDs")
    parser.add_argument("--csv", type=Path, help="CSV whose first column lists people")
    parser.add_argument("--group", help="Developer group object ID; defaults to the deployment's")
    parser.add_argument("--state", type=Path, default=Path(".local/deploy-state.json"))
    parser.add_argument("--remove", action="store_true", help="Remove access instead")
    parser.add_argument("--portal-csv", type=Path, default=Path(".local/portal-members.csv"))
    args = parser.parse_args()
    state = read_json(args.state, default={})
    group = args.group or state.get("developer_group_id")
    if not group:
        parser.error("Run scripts/deploy.py first or pass --group")
    people = identities(args.people, args.csv)
    if not people:
        parser.error("Name at least one person or pass --csv")
    try:
        results = manage(people, group, args.remove)
    except RuntimeError as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1
    for r in results:
        print(
            f"{r['action']:<19} {r.get('email', r['identity'])} {r.get('object_id', '')}".rstrip()
        )
    if not args.remove:
        write_portal_csv(args.portal_csv, results)
        print(
            f"\nAccess reaches Foundry within a few minutes. Send the installer: {state.get('bundle', 'dist/*-macos.zip')}"
        )
        print(
            f"If portal governance is published, import {args.portal_csv} under Members and publish."
        )
    return 1 if any(r["action"] == "not found" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
