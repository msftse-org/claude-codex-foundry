import copy
from pathlib import Path

import pytest
import tomlkit

from claude_foundry.codex_client import catalog_for, install, restore
from claude_foundry.codex_models import initialize
from claude_foundry.config import export_client


@pytest.fixture
def codex(org):
    c = initialize(org)
    c.update(
        default_model="gpt-5.4", models=[{"name": "gpt-5.4", "model": "gpt-5.4", "qualified": True}]
    )
    return export_client(c)


@pytest.fixture
def catalog():
    return {
        "models": [
            {
                "slug": "gpt-5.4",
                "base_instructions": "Test only",
                "apply_patch_tool_type": "freeform",
                "supported_reasoning_levels": [{"effort": "low", "description": "Low"}],
                "upgrade": {"id": "unrouted-model"},
                "service_tiers": ["fast"],
            }
        ]
    }


def test_install_restore_preserves_history_credentials_trust_and_comments(codex, catalog, tmp_path):
    root = tmp_path / ".codex"
    root.mkdir()
    original = '# Keep my comment\nmodel = "original"\napproval_policy = "on-request"\n[projects."/my-project"]\ntrust_level = "trusted"\n'
    config = root / "config.toml"
    config.write_text(original)
    history = root / "sessions/old.jsonl"
    history.parent.mkdir()
    history.write_text("original history")
    credentials = root / "auth.json"
    credentials.write_text("original credentials")
    first = install(
        codex, home=tmp_path, executable="/reviewed path/bin/claude-foundry", catalog=catalog
    )
    assert "# Keep my comment" in config.read_text()
    doc = tomlkit.parse(config.read_text())
    assert doc["approval_policy"] == "on-request"
    assert doc["model_provider"] == "azure-foundry"
    assert "token --config" in Path(first["helper"]).read_text()
    doc["model"] = "later-choice"
    doc["projects"]["/another"] = {"trust_level": "untrusted"}
    config.write_text(tomlkit.dumps(doc))
    restore(tmp_path)
    result = tomlkit.parse(config.read_text())
    assert result["model"] == "later-choice"
    assert "model_provider" not in result
    assert result["projects"]["/another"]["trust_level"] == "untrusted"
    assert history.read_text() == "original history"
    assert credentials.read_text() == "original credentials"
    assert "# Keep my comment" in config.read_text()


def test_reinstall_preserves_new_user_setting_for_restore(codex, catalog, tmp_path):
    install(codex, home=tmp_path, catalog=catalog)
    config = tmp_path / ".codex/config.toml"
    doc = tomlkit.parse(config.read_text())
    doc["model_reasoning_effort"] = "high"
    config.write_text(tomlkit.dumps(doc))
    install(codex, home=tmp_path, catalog=catalog)
    restore(tmp_path)
    assert tomlkit.parse(config.read_text())["model_reasoning_effort"] == "high"


def test_edited_provider_is_not_silently_overwritten(codex, catalog, tmp_path):
    install(codex, home=tmp_path, catalog=catalog)
    config = tmp_path / ".codex/config.toml"
    doc = tomlkit.parse(config.read_text())
    doc["model_providers"]["azure-foundry"]["base_url"] = "https://other.example/v1"
    config.write_text(tomlkit.dumps(doc))
    before = config.read_bytes()
    with pytest.raises(ValueError, match="edited"):
        install(codex, home=tmp_path, catalog=catalog)
    assert config.read_bytes() == before


def test_unqualified_or_foreign_catalog_fails_before_mutation(codex, catalog, tmp_path):
    codex["models"][0]["qualified"] = False
    with pytest.raises(ValueError, match="qualified"):
        install(codex, home=tmp_path, catalog=catalog)
    assert not list(tmp_path.iterdir())
    codex["models"][0]["qualified"] = True
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('model_catalog_json="/customer/catalog.json"\n')
    with pytest.raises(ValueError, match="custom Codex catalog"):
        install(codex, home=tmp_path, catalog=catalog)


def test_catalog_disables_unrouted_features_and_keeps_vendor_source(codex, catalog):
    original = copy.deepcopy(catalog)
    codex["models"].append({"name": "legacy", "model": "gpt-4.1", "qualified": True})
    entries = catalog_for(codex, catalog)["models"]
    assert catalog == original
    assert all(
        m["upgrade"] is None and not m["service_tiers"] and not m["use_responses_lite"]
        for m in entries
    )
    assert entries[1]["apply_patch_tool_type"] is None
    assert not entries[1]["supported_reasoning_levels"]
    assert entries[1]["context_window"] == 32768
