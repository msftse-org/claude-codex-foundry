import csv
import importlib.util
from pathlib import Path

import pytest

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
