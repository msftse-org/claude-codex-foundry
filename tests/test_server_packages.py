import importlib.util
import os
import stat
import zipfile
from pathlib import Path

import pytest


@pytest.mark.parametrize("kind", ["portal", "budget"])
def test_public_server_package_is_readable_under_a_private_build_umask(kind, tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / f"scripts/build_{kind}.py"
    spec = importlib.util.spec_from_file_location(f"build_{kind}_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: b"locked requirements")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: None)
    if kind == "portal":
        monkeypatch.setattr(module, "build_template", lambda root: b"public installer template")
    root = tmp_path / "source"
    files = {
        f"{kind}/functions/function_app.py": "# public function code\n",
        f"{kind}/functions/host.json": "{}\n",
        "src/claude_foundry/__init__.py": "# public helper code\n",
    }
    if kind == "portal":
        files.update(
            {
                "portal/web/dist/index.html": "<html>public portal</html>",
                "portal/web/dist/assets/app.js": "// public javascript",
            }
        )
    previous = os.umask(0o077)
    try:
        for filename, content in files.items():
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        output = module.build(tmp_path / "server.zip", root)
    finally:
        os.umask(previous)
    with zipfile.ZipFile(output) as package:
        for entry in package.infolist():
            assert entry.create_system == 3
            assert stat.S_IMODE(entry.external_attr >> 16) == 0o644
        assert (
            package.read("function_app.py") == files[f"{kind}/functions/function_app.py"].encode()
        )
        assert not any(".local" in name for name in package.namelist())
    # Packaging must not make an operator's local files world-readable.
    assert all(stat.S_IMODE((root / filename).stat().st_mode) == 0o600 for filename in files)
