import csv
import importlib.util
import sys
from pathlib import Path

import pytest

from claude_foundry.config import write_json

ROOT = Path(__file__).resolve().parents[1]
GROUP = "00000000-0000-0000-0000-000000000009"
ALICE = "00000000-0000-0000-0000-00000000000a"
GUEST = "00000000-0000-0000-0000-00000000000b"


@pytest.fixture
def onboard():
    spec = importlib.util.spec_from_file_location("onboard", ROOT / "scripts/onboard_users.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Directory:
    def __init__(self, members=()):
        self.members = set(members)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:3] == ("ad", "user", "show"):
            if args[4] in {"alice@contoso.com", ALICE}:
                return {
                    "id": ALICE,
                    "displayName": "Alice",
                    "userPrincipalName": "alice@contoso.com",
                }
            raise RuntimeError("Resource does not exist")
        if args[:3] == ("ad", "user", "list"):
            if "guest@partner.com" in args[4]:
                return [{"id": GUEST, "displayName": "Guest", "mail": "guest@partner.com"}]
            return []
        if args[:4] == ("ad", "group", "member", "check"):
            return {"value": args[-1] in self.members}
        if args[:4] == ("ad", "group", "member", "add"):
            self.members.add(args[-1])
        if args[:4] == ("ad", "group", "member", "remove"):
            self.members.discard(args[-1])
        return None


def test_adds_members_by_upn_and_guest_email_once(onboard):
    directory = Directory(members={GUEST})
    results = onboard.manage(
        ["alice@contoso.com", "guest@partner.com", "nobody@contoso.com"], GROUP, run=directory
    )
    assert [r["action"] for r in results] == ["added", "already has access", "not found"]
    assert directory.members == {ALICE, GUEST}
    adds = [c for c in directory.calls if c[:4] == ("ad", "group", "member", "add")]
    assert adds == [("ad", "group", "member", "add", "--group", GROUP, "--member-id", ALICE)]


def test_removal_only_touches_existing_members(onboard):
    directory = Directory(members={ALICE})
    results = onboard.manage([ALICE, "guest@partner.com"], GROUP, remove=True, run=directory)
    assert [r["action"] for r in results] == ["removed", "not a member"]
    assert directory.members == set()


def test_unknown_object_id_is_reported_without_mail_lookup(onboard):
    directory = Directory()
    unknown = "00000000-0000-0000-0000-0000000000ff"
    results = onboard.manage([unknown], GROUP, run=directory)
    assert results[0]["action"] == "not found"
    assert not any(c[:3] == ("ad", "user", "list") for c in directory.calls)


def test_csv_input_and_portal_import_file(onboard, tmp_path):
    source = tmp_path / "people.csv"
    source.write_text("email\nalice@contoso.com\n\nalice@contoso.com\nguest@partner.com\n")
    people = onboard.identities(["bob@contoso.com"], source)
    assert people == ["bob@contoso.com", "alice@contoso.com", "guest@partner.com"]
    results = onboard.manage(people, GROUP, run=Directory())
    output = tmp_path / "portal.csv"
    onboard.write_portal_csv(output, results)
    with open(output, newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["object_id"] for r in rows] == [ALICE, GUEST]
    assert set(rows[0]) == {"object_id", "name", "email"}


def test_wrong_tenant_stops_before_directory_mutations(onboard, tmp_path, monkeypatch, capsys):
    state = tmp_path / "state.json"
    write_json(state, {"tenant_id": ALICE, "developer_group_id": GROUP})
    calls = []

    def run(*args):
        calls.append(args)
        return {"tenantId": GUEST}

    monkeypatch.setattr(onboard, "az", run)
    monkeypatch.setattr(sys, "argv", ["onboard_users.py", "--state", str(state), ALICE])
    assert onboard.main() == 1
    assert calls == [("account", "show")]
    assert "Wrong tenant" in capsys.readouterr().err


BOB = "00000000-0000-0000-0000-00000000000c"


class Portal:
    """The portal's audited change API as the signed-in owner sees it."""

    def __init__(self, members=(), groups=("Platform team",), published=True):
        self.groups = [{"id": f"group-{i}", "name": name} for i, name in enumerate(groups)]
        self.members = {m["id"]: m for m in members}
        self.revision, self.published, self.changes = 3, published, []

    def state(self):
        return {
            "revision": self.revision,
            "groups": self.groups,
            "members": list(self.members.values()),
        }

    def change(self, revision, kind, value):
        assert revision == self.revision
        self.changes.append((kind, value))
        self.revision += 1
        note = None if self.published else "Saved as a draft: group not published."
        return {"revision": self.revision, "published": self.published, "publish_note": note}


def person(oid, name):
    return {"identity": name, "action": "added", "object_id": oid, "name": name, "email": name}


def test_unified_onboarding_makes_people_live_portal_members(onboard):
    suspended = {"id": BOB, "name": "Bob", "status": "suspended", "group_id": "group-0"}
    active = {"id": GUEST, "name": "Guest", "status": "active", "group_id": "group-0"}
    portal = Portal(members=[suspended, active])
    people = [person(ALICE, "alice@contoso.com"), person(BOB, "bob@contoso.com")]
    people.append(person(GUEST, "guest@partner.com"))
    people.append({"identity": "nobody@contoso.com", "action": "not found"})
    results = onboard.portal_access(portal, people)
    assert [(r.get("portal"), r.get("published")) for r in results] == [
        ("added", True),
        ("reactivated", True),
        ("member", True),
        (None, None),
    ]
    kinds = [kind for kind, _ in portal.changes]
    assert kinds == ["member", "member_import"]
    assert portal.changes[0][1]["status"] == "active"
    imported = portal.changes[1][1]
    assert imported["group_id"] == "group-0"
    assert imported["csv"].splitlines()[1:] == [f"{ALICE},alice@contoso.com,alice@contoso.com"]


def test_new_members_need_a_named_group_when_there_are_several(onboard):
    portal = Portal(groups=("Platform team", "Data science"))
    with pytest.raises(ValueError, match="Data science, Platform team"):
        onboard.portal_access(portal, [person(ALICE, "alice@contoso.com")])
    onboard.portal_access(portal, [person(ALICE, "alice@contoso.com")], "Data science")
    assert portal.changes[0][1]["group_id"] == "group-1"


def test_unified_removal_suspends_and_reports_drafts(onboard):
    active = {"id": ALICE, "name": "Alice", "status": "active", "group_id": "group-0"}
    portal = Portal(members=[active], published=False)
    results = onboard.portal_access(
        portal, [person(ALICE, "alice"), person(GUEST, "guest")], remove=True
    )
    assert [(r["portal"], r["published"]) for r in results] == [
        ("suspended", False),
        ("not a member", True),
    ]
    assert results[0]["note"] == "Saved as a draft: group not published."
    assert portal.changes == [("member", {**active, "status": "suspended"})]


def test_unified_main_reports_live_access(onboard, tmp_path, monkeypatch, capsys):
    state = tmp_path / "deploy-state.json"
    write_json(
        state,
        {
            "tenant_id": ALICE,
            "developer_group_id": GROUP,
            "governance_mode": "unified",
            "portal_url": "https://ccf-admin-test.azurewebsites.net",
        },
    )
    write_json(tmp_path / "portal/entra.json", {"client_id": GUEST})
    portal = Portal()
    monkeypatch.setattr(onboard, "az", lambda *args: {"tenantId": ALICE})
    monkeypatch.setattr(onboard, "manage", lambda *args: [person(ALICE, "alice@contoso.com")])
    monkeypatch.setattr(onboard, "PortalClient", lambda url, client, tenant: portal)
    monkeypatch.setattr(sys, "argv", ["onboard_users.py", "--state", str(state), ALICE])
    assert onboard.main() == 0
    out = capsys.readouterr().out
    assert "portal: added" in out and "(access live)" in out
    assert "https://ccf-admin-test.azurewebsites.net/?page=onboarding" in out
    assert "reaches Foundry" not in out
