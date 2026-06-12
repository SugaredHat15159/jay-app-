"""Self-update via GitHub Releases. Works with a PUBLIC repo (no auth needed).

The app compares its __version__ to the latest release tag and, if a newer
release exists with a Setup .exe asset, downloads and runs the installer.
"""
import json
import os
import subprocess
import tempfile
import urllib.request

GITHUB_REPO = "SugaredHat15159/jay-app-"   # owner/repo  (edit if it changes)
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
_HEADERS = {"User-Agent": "jay-pc-agent", "Accept": "application/vnd.github+json"}


def parse_version(v):
    v = (v or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    out = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def is_newer(remote_tag, local_version):
    return parse_version(remote_tag) > parse_version(local_version)


def check_latest(timeout=8):
    """Return {'tag':..., 'asset_url':...} for the latest release, or None."""
    req = urllib.request.Request(API_LATEST, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    tag = data.get("tag_name")
    if not tag:
        return None
    asset_url = None
    for a in data.get("assets", []):
        name = (a.get("name") or "").lower()
        if name.endswith(".exe") and "setup" in name:
            asset_url = a.get("browser_download_url")
            break
    return {"tag": tag, "asset_url": asset_url}


def download_installer(asset_url, timeout=300):
    req = urllib.request.Request(asset_url, headers={"User-Agent": "jay-pc-agent"})
    fd, path = tempfile.mkstemp(suffix="_JAY-PC-Agent-Setup.exe")
    os.close(fd)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
    return path


def launch_installer(installer_path):
    """Start the installer; the app should quit right after so it can update."""
    subprocess.Popen([installer_path])
