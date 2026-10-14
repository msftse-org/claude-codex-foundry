import copy
import json

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
