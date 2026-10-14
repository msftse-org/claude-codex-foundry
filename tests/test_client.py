import copy
import json
from pathlib import Path

import pytest

from claude_foundry.client import install, restore
from claude_foundry.config import export_client, read_json, write_json


def test_install_reinstall_restore_preserves_user_changes(org, tmp_path):
    c = export_client(org)
    path = tmp_path / ".claude/settings.json"
    original = {
        "model": "opus",
        "env": {"MY_SETTING": "keep", "ANTHROPIC_API_KEY": "old-test-key"},
        "permissions": {"allow": ["Read"]},
    }
    write_json(path, original)
    first = install(c, home=tmp_path, executable="/reviewed path/bin/claude-foundry")
    changed = read_json(path)
    changed["permissions"]["allow"].append("Glob")
    write_json(path, changed)
    second = install(c, home=tmp_path)
    assert first["profile"] == second["profile"]
    assert "ANTHROPIC_API_KEY" not in read_json(path)["env"]
    assert (
        len(
            read_json(tmp_path / "Library/Application Support/Claude-3p/configLibrary/_meta.json")[
                "entries"
            ]
        )
        == 1
    )
    restore(tmp_path)
    result = read_json(path)
    assert result["env"] == original["env"]
    assert result["model"] == "opus"
    assert result["permissions"]["allow"] == ["Read", "Glob"]


def test_restore_preserves_new_profile_and_user_model(org, tmp_path):
    info = install(export_client(org), home=tmp_path)
    path = tmp_path / ".claude/settings.json"
    value = read_json(path)
    value["model"] = "users-later-model"
    write_json(path, value)
    meta = tmp_path / "Library/Application Support/Claude-3p/configLibrary/_meta.json"
    value = read_json(meta)
    value["entries"].append({"id": "another-profile", "name": "Keep this"})
    write_json(meta, value)
    restore(tmp_path)
    assert read_json(path)["model"] == "users-later-model"
    assert read_json(meta)["entries"] == [{"id": "another-profile", "name": "Keep this"}]
    from pathlib import Path

    assert not Path(info["profile"]).exists()


def test_desktop_model_catalog_encodes_as_json_string(org, tmp_path):
    c = export_client(org)
    info = install(c, home=tmp_path)
    from pathlib import Path

    settings = read_json(Path(info["profile"]))
    assert json.loads(settings["inferenceModels"]) == c["models"]
    assert settings["inferenceGatewayAuthScheme"] == "bearer"
    assert settings["isClaudeCodeForDesktopEnabled"] == "true"


def test_desktop_omits_http_catalog_metadata(org, tmp_path):
    from pathlib import Path

    c = export_client(org)
    c["models"][0]["created_at"] = "2026-10-14T00:00:00Z"
    info = install(c, home=tmp_path)
    models = json.loads(read_json(Path(info["profile"]))["inferenceModels"])
    assert "created_at" not in models[0]
    assert c["models"][0]["created_at"] == "2026-10-14T00:00:00Z"


def test_invalid_profile_cannot_mutate_settings(org, tmp_path):
    import pytest

    c = copy.deepcopy(export_client(org))
    c["models"][0]["name"] = "../invalid"
    with pytest.raises(ValueError):
        install(c, home=tmp_path)
    assert not list(tmp_path.iterdir())


def test_history_import_is_opt_in_and_never_edits_conversations(org, tmp_path):
    c = export_client(org)
    histories = [
        tmp_path / "Library/Application Support/Claude/local-agent-mode-sessions/history.json",
        tmp_path / "Library/Application Support/Claude-3p/local-agent-mode-sessions/history.json",
        tmp_path / ".claude/projects/project/transcript.jsonl",
    ]
    for path in histories:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"existing conversation\n")
    first = install(c, home=tmp_path)
    profile = Path(first["profile"])
    assert "claudeAiImport" not in read_json(profile)
    c["conversation_import"] = {"enabled": True}
    install(c, home=tmp_path)
    policy = json.loads(read_json(profile)["claudeAiImport"])
    assert policy["enabled"] is True
    assert policy["automatic3pImport"] is False
    assert policy["exportEnabled"] is False
    assert policy["bannerBehavior"] == "detect"
    restore(tmp_path)
    assert not profile.exists()
    assert all(path.read_bytes() == b"existing conversation\n" for path in histories)


@pytest.mark.parametrize("source_ids", [[], ["00000000-0000-0000-0000-000000000003"]])
def test_enable_history_preserves_existing_source_restrictions(org, tmp_path, source_ids):
    c = export_client(org)
    profile = Path(install(c, home=tmp_path)["profile"])
    existing = {"enabled": False, "allowedOrganizationUuids": source_ids}
    settings = read_json(profile)
    settings["claudeAiImport"] = json.dumps(existing)
    write_json(profile, settings)
    c["conversation_import"] = {"enabled": True}
    install(c, home=tmp_path)
    actual = json.loads(read_json(profile)["claudeAiImport"])
    assert actual["allowedOrganizationUuids"] == source_ids
    assert actual["enabled"] is True
    assert "allowedOrganizationUuids" not in c["conversation_import"]
    restore(tmp_path)
    assert json.loads(read_json(profile)["claudeAiImport"]) == existing


def test_import_policy_restore_preserves_later_customer_changes(org, tmp_path):
    c = export_client(org)
    c["conversation_import"] = {"enabled": True}
    profile = Path(install(c, home=tmp_path)["profile"])
    settings = read_json(profile)
    settings["claudeAiImport"] = json.dumps({"enabled": False, "bannerBehavior": "off"})
    write_json(profile, settings)
    restore(tmp_path)
    assert read_json(profile)["claudeAiImport"] == settings["claudeAiImport"]


def test_import_never_redirects_an_existing_custom_export_endpoint(org, tmp_path):
    c = export_client(org)
    profile = Path(install(c, home=tmp_path)["profile"])
    settings = read_json(profile)
    settings["claudeAiImport"] = json.dumps({"url": "https://export.example.com"})
    write_json(profile, settings)
    before = profile.read_bytes()
    c["conversation_import"] = {"enabled": True}
    with pytest.raises(ValueError, match="custom history import endpoint"):
        install(c, home=tmp_path)
    assert profile.read_bytes() == before


def test_mdm_import_policy_cannot_be_overridden(monkeypatch, tmp_path):
    import plistlib

    from claude_foundry import client

    managed = tmp_path / "Library/Managed Preferences/com.anthropic.claudefordesktop.plist"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(plistlib.dumps({"claudeAiImport": {"enabled": False}}))
    monkeypatch.setattr(client, "Path", lambda path: tmp_path / path.lstrip("/"))
    with pytest.raises(RuntimeError, match="MDM administrator"):
        client.check_managed_preferences()


@pytest.mark.parametrize("version", ["1.99999.0", "2.26453.9", "unknown", 123])
def test_import_rejects_builds_without_source_policy_support(tmp_path, version):
    import plistlib

    from claude_foundry.client import check_desktop_import_version

    path = tmp_path / "Info.plist"
    path.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
    with pytest.raises(RuntimeError, match="2.26454.0"):
        check_desktop_import_version(path)


def test_import_accepts_build_with_source_policy_support(tmp_path):
    import plistlib

    from claude_foundry.client import check_desktop_import_version

    path = tmp_path / "Info.plist"
    path.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "2.26454.0"}))
    check_desktop_import_version(path)


@pytest.mark.parametrize(
    "policy",
    [
        True,
        {},
        {"enabled": "true"},
        {"enabled": True, "automatic3pImport": True},
        {"enabled": True, "url": "https://export.example.com"},
        {"enabled": True, "bannerBehavior": []},
        {"enabled": True, "allowedOrganizationUuids": "not-a-list"},
        {"enabled": True, "allowedOrganizationUuids": ["invalid"]},
        {"enabled": True, "allowedOrganizationUuids": ["00000000-0000-0000-0000-000000000001"] * 2},
    ],
)
def test_invalid_import_policy_cannot_mutate_settings(org, tmp_path, policy):
    c = export_client(org)
    c["conversation_import"] = policy
    with pytest.raises(ValueError):
        install(c, home=tmp_path)
    assert not list(tmp_path.iterdir())
