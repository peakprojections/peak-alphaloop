"""Check GitHub Releases for a newer version (no account or token needed).

Release assets are named ``PeakAlphaloop-<version>-<platform>`` (see
``ASSET_SUFFIXES``) and every release carries ``SHA256SUMS.txt``. The Windows
build can update itself: it downloads the new per-user installer, verifies its
checksum, and runs it silently. Other platforms are pointed at the download.

Set ``ALPHALOOP_NO_UPDATE_CHECK=1`` (or pass ``--no-update-check``) to turn the
check off. The only request made is an anonymous GET to api.github.com.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

REPOSITORY = "peakprojections/peak-alphaloop"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
CHECKSUMS_NAME = "SHA256SUMS.txt"
ASSET_SUFFIXES = {
    "windows": "windows-x64-setup.exe",
    "macos": "macos-arm64.zip",
    "macos-intel": "macos-x64.zip",
    "linux": "linux-x64.tar.xz",
}
CACHE_SECONDS = 6 * 3600
TIMEOUT = 8

_cache: tuple[float, dict[str, Any]] | None = None
_cache_lock = threading.Lock()


class UpdateError(RuntimeError):
    pass


def is_standalone() -> bool:
    return bool(getattr(sys, "frozen", False))


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos-intel" if platform.machine() == "x86_64" else "macos"
    return "linux"


def parse_version(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", text.split("-", 1)[0].split("+", 1)[0])
    return tuple(int(n) for n in numbers[:4]) or (0,)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # bundled with the standalone builds
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    request = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": f"PeakAlphaloop/{__version__} ({platform.system()})",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=_ssl_context()) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise UpdateError(f"Could not reach GitHub: {error}") from error


def check() -> dict[str, Any]:
    """Compare this version with the latest GitHub release."""
    result: dict[str, Any] = {"current": __version__, "available": False,
                              "page": RELEASES_PAGE, "platform": platform_key(),
                              "standalone": is_standalone(), "can_install": False}
    if os.environ.get("ALPHALOOP_NO_UPDATE_CHECK"):
        result["disabled"] = True
        return result
    try:
        release = json.loads(_get(RELEASES_API))
    except (UpdateError, json.JSONDecodeError) as error:
        result["error"] = str(error)
        return result
    tag = str(release.get("tag_name", ""))
    result["latest"] = tag.lstrip("vV")
    result["page"] = release.get("html_url") or RELEASES_PAGE
    result["notes"] = str(release.get("body") or "")[:4000]
    result["available"] = parse_version(tag) > parse_version(__version__)
    suffix = ASSET_SUFFIXES[platform_key()]
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        if name.endswith(suffix):
            result["asset"] = {"name": name, "url": asset.get("browser_download_url"),
                               "size": asset.get("size")}
        elif name == CHECKSUMS_NAME:
            result["checksums_url"] = asset.get("browser_download_url")
    result["can_install"] = bool(result["available"] and is_standalone()
                                 and platform_key() == "windows" and "asset" in result
                                 and "checksums_url" in result)
    return result


def check_cached() -> dict[str, Any]:
    global _cache
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    result = check()
    with _cache_lock:
        _cache = (time.monotonic(), result)
    return result


def expected_checksum(checksums: str, asset_name: str) -> str:
    for line in checksums.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset_name:
            return parts[0].lower()
    raise UpdateError(f"{asset_name} is not listed in {CHECKSUMS_NAME}.")


def install_update() -> str:
    """Windows build only: download the installer, verify it, run it silently, quit.

    The installer closes this app, replaces it, and starts the new version.
    """
    info = check()
    if not info.get("can_install"):
        raise UpdateError(f"Download the new version from {info.get('page', RELEASES_PAGE)}")
    asset = info["asset"]
    expected = expected_checksum(_get(info["checksums_url"], "application/octet-stream")
                                 .decode("utf-8", "replace"), asset["name"])
    folder = Path(tempfile.mkdtemp(prefix="alphaloop-update-"))
    installer = folder / asset["name"]
    installer.write_bytes(_get(asset["url"], "application/octet-stream"))
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    if digest != expected:
        installer.unlink(missing_ok=True)
        raise UpdateError("The downloaded update did not match its published checksum; "
                          "nothing was changed.")
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen([str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                      "/CLOSEAPPLICATIONS"], close_fds=True, creationflags=flags)
    return f"Installing {info['latest']}; Peak Alphaloop will reopen when it's done."


def cleanup_previous_install() -> None:
    """Remove downloaded installers from earlier self-updates."""
    if not is_standalone():
        return
    cutoff = time.time() - 3600
    for folder in Path(tempfile.gettempdir()).glob("alphaloop-update-*"):
        try:
            if folder.stat().st_mtime < cutoff:
                for item in folder.iterdir():
                    item.unlink(missing_ok=True)
                folder.rmdir()
        except OSError:
            pass
