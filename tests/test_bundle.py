import importlib.util
import zipfile
from pathlib import Path

import pytest

from claude_foundry.config import export_client, write_json


def builder():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("bundle", root / "scripts/build_client_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build, root


def test_distribution_excludes_administrator_state(org, tmp_path):
    build, root = builder()
    profile = tmp_path / "client.json"
    write_json(profile, export_client(org))
    output = build(profile, tmp_path / "client.zip", root)
    with zipfile.ZipFile(output) as bundle:
        names = bundle.namelist()
        assert "claude-foundry/client.json" in names
        assert not any(
            "/.local/" in name or "/.git/" in name or "/infra/" in name for name in names
        )
        assert bundle.getinfo("claude-foundry/Install.command").external_attr >> 16 & 0o111


def test_distribution_refuses_an_admin_profile(org, tmp_path):
    build, root = builder()
    profile = tmp_path / "organization.json"
    write_json(profile, org)
    with pytest.raises(ValueError, match="client profile only"):
        build(profile, tmp_path / "client.zip", root)
    assert not (tmp_path / "client.zip").exists()
