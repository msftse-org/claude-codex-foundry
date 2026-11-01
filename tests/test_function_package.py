import io
import json
import zipfile

import pytest

from claude_foundry import function_package


def package(path, files):
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
    return path


def test_stamp_names_the_package_by_its_content(tmp_path):
    first = package(tmp_path / "a.zip", {"function_app.py": "app", "host.json": "{}"})
    second = package(tmp_path / "b.zip", {"host.json": "{}", "function_app.py": "app"})
    changed = package(tmp_path / "c.zip", {"function_app.py": "app2", "host.json": "{}"})
    build = function_package.stamp(first)
    assert function_package.stamp(second) == build
    assert function_package.stamp(changed) != build
    assert function_package.package_build(first) == build
    with zipfile.ZipFile(first) as bundle:
        entry = bundle.getinfo("build.json")
        assert entry.external_attr >> 16 == 0o100644
        assert json.loads(bundle.read(entry)) == {"build": build}
    with pytest.raises(ValueError, match="already"):
        function_package.stamp(first)


def test_build_reads_the_serving_package_or_reports_development(tmp_path):
    assert function_package.read_build(tmp_path) == "development"
    (tmp_path / "build.json").write_text('{"build": "abc123"}')
    assert function_package.read_build(tmp_path) == "abc123"


def test_publish_uploads_restarts_then_waits_for_that_build(tmp_path, monkeypatch):
    path = package(tmp_path / "app.zip", {"function_app.py": "app"})
    build = function_package.stamp(path)
    calls, waited = [], []
    monkeypatch.setattr(function_package, "RESTART_SECONDS", 0)
    monkeypatch.setattr(
        function_package, "wait_for_build", lambda url, served: waited.append((url, served))
    )
    function_package.publish(calls.append, "rg", "ccf-admin-x", path, "https://x/api/health")
    assert [c[1:4] for c in calls] == [
        ["functionapp", "deployment", "source"],
        ["functionapp", "stop", "-g"],
        ["functionapp", "start", "-g"],
    ]
    assert "config-zip" in calls[0] and "ccf-admin-x" in calls[1] and "ccf-admin-x" in calls[2]
    assert waited == [("https://x/api/health", build)]


def test_waiting_fails_while_the_old_build_still_serves(monkeypatch):
    monkeypatch.setattr(function_package.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        function_package,
        "urlopen",
        lambda url, timeout: io.BytesIO(json.dumps({"build": "old"}).encode()),
    )
    with pytest.raises(TimeoutError, match="still serves build old, not the deployed new"):
        function_package.wait_for_build("https://x/health", "new", attempts=3, delay=0)
    function_package.wait_for_build("https://x/health", "old", attempts=1, delay=0)
