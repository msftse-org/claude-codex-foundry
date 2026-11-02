"""Ship Function packages so that the build uploaded is the build that serves.

Flex Consumption keeps warm workers on the previous package after a successful
deployment. Each package carries a content-derived build ID; deployment stops the
app for longer than a minute, starts it, and waits until /health reports that ID.
"""

import hashlib
import json
import time
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

BUILD_FILE = "build.json"
# A stop shorter than about a minute can resume the previous warm worker.
RESTART_SECONDS = 75


def stamp(path):
    """Append build.json naming the package's content digest; return the build ID."""
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as bundle:
        for info in sorted(bundle.infolist(), key=lambda entry: entry.filename):
            if info.filename == BUILD_FILE:
                raise ValueError("This package already has a build ID")
            data = bundle.read(info)
            digest.update(f"{info.filename}\0{len(data)}\0".encode() + data)
    build = digest.hexdigest()[:16]
    entry = zipfile.ZipInfo(BUILD_FILE)
    entry.create_system = 3
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o100644 << 16
    with zipfile.ZipFile(path, "a") as bundle:
        bundle.writestr(entry, json.dumps({"build": build}))
    return build


def package_build(path):
    with zipfile.ZipFile(path) as bundle:
        return json.loads(bundle.read(BUILD_FILE))["build"]


def read_build(root):
    """The serving package's build ID, or "development" outside a package."""
    try:
        return json.loads((Path(root) / BUILD_FILE).read_text())["build"]
    except (OSError, ValueError, KeyError):
        return "development"


def restart(run, group, name):
    print(
        f"Restarting {name} so it loads the new package; requests fail for about two minutes.",
        flush=True,
    )
    run(["az", "functionapp", "stop", "-g", group, "-n", name, "--only-show-errors", "-o", "none"])
    time.sleep(RESTART_SECONDS)
    run(["az", "functionapp", "start", "-g", group, "-n", name, "--only-show-errors", "-o", "none"])


def wait_for_build(url, build, attempts=60, delay=10):
    seen = None
    for _ in range(attempts):
        try:
            with urlopen(url, timeout=30) as response:
                seen = json.load(response).get("build")
            if seen == build:
                print(f"Serving build {build}: {url}", flush=True)
                return
        except (URLError, TimeoutError, ValueError, AttributeError):
            pass
        time.sleep(delay)
    raise TimeoutError(f"{url} still serves build {seen or 'unknown'}, not the deployed {build}")


def publish(run, group, name, package, health_url):
    """Upload with remote build, restart onto it and confirm the new build serves."""
    build = package_build(package)
    run(
        [
            "az",
            "functionapp",
            "deployment",
            "source",
            "config-zip",
            "-g",
            group,
            "-n",
            name,
            "--src",
            package,
            "--build-remote",
            "true",
            "--timeout",
            "900",
            "--only-show-errors",
            "-o",
            "none",
        ]
    )
    restart(run, group, name)
    wait_for_build(health_url, build)
    return build
