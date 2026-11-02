"""Build the Peak Alphaloop standalone download for the current OS.

    python packaging/build_standalone.py            # in CI: downloads FFmpeg too
    python packaging/build_standalone.py --ffmpeg-dir /usr/bin   # use a local FFmpeg

Produces one file in ``release/``:

    PeakAlphaloop-<version>-windows-x64-setup.exe   (per-user installer)
    PeakAlphaloop-<version>-macos-arm64.zip         (Peak Alphaloop.app)
    PeakAlphaloop-<version>-linux-x64.tar.gz        (folder with the app)

and finishes by running the built app's ``--self-test``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
RELEASE = ROOT / "release"
APP = "PeakAlphaloop"
sys.path.insert(0, str(ROOT))
from peak_alphaloop import __version__ as VERSION  # noqa: E402
from peak_alphaloop.updater import ASSET_SUFFIXES  # noqa: E402

GPL_URL = "https://www.gnu.org/licenses/gpl-3.0.txt"
MAC_FFMPEG = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/{}.zip"


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("   $ " + " ".join(str(part) for part in command), flush=True)
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def download(url: str, destination: Path) -> Path:
    log(f"Downloading {url}")
    headers = {"User-Agent": "peak-alphaloop-build"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=300) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)
    return destination


# --------------------------------------------------------------------------- FFmpeg

def btbn_asset(target: str, variant: str) -> str:
    """Newest release-branch BtbN build URL (falls back to the master build)."""
    api = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
    with tempfile.TemporaryDirectory() as folder:
        data = json.loads(download(api, Path(folder) / "release.json").read_text())
    pattern = re.compile(rf"^ffmpeg-n(\d+(?:\.\d+)*)-latest-{target}-gpl{variant}-[\d.]+\.(zip|tar\.xz)$")
    best = None
    for asset in data.get("assets", []):
        match = pattern.match(asset["name"])
        if match:
            version = tuple(int(n) for n in match.group(1).split("."))
            if best is None or version > best[0]:
                best = (version, asset["browser_download_url"])
    if best:
        return best[1]
    extension = "zip" if target.startswith("win") else "tar.xz"
    return ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
            f"ffmpeg-master-latest-{target}-gpl{variant}.{extension}")


def fetch_ffmpeg(key: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        if key == "macos":
            for name in ("ffmpeg", "ffprobe"):
                archive = download(MAC_FFMPEG.format(name), work / f"{name}.zip")
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(work / name)
                binary = next(path for path in (work / name).rglob(name) if path.is_file())
                shutil.copy2(binary, target / name)
                (target / name).chmod(0o755)
            return
        if key == "windows":
            # Shared build: one set of DLLs serves both ffmpeg.exe and ffprobe.exe.
            archive = download(btbn_asset("win64", "-shared"), work / "ffmpeg.zip")
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(work / "x")
        else:
            archive = download(btbn_asset("linux64", ""), work / "ffmpeg.tar.xz")
            with tarfile.open(archive) as bundle:
                bundle.extractall(work / "x")
        bin_dir = next(path for path in (work / "x").rglob("bin") if path.is_dir())
        for item in bin_dir.iterdir():
            if item.is_file() and (item.suffix.lower() in {".exe", ".dll"} or key == "linux"):
                if item.stem in {"ffplay"}:
                    continue
                shutil.copy2(item, target / item.name)
                (target / item.name).chmod(0o755)


def copy_local_ffmpeg(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        found = shutil.which(name, path=str(source)) or shutil.which(name)
        if not found:
            raise SystemExit(f"{name} not found in {source}")
        shutil.copy2(Path(found).resolve(), target / Path(found).name)


def write_licenses(ffmpeg_dir: Path, target: Path, gpl_text: Path | None) -> None:
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", target / "LICENSE.txt")
    shutil.copy2(ROOT / "packaging" / "THIRD_PARTY_NOTICES.md", target / "THIRD_PARTY_NOTICES.md")
    if gpl_text and gpl_text.is_file():
        shutil.copy2(gpl_text, target / "GPL-3.0.txt")
    else:
        download(GPL_URL, target / "GPL-3.0.txt")
    ffmpeg = next(path for path in ffmpeg_dir.iterdir() if path.stem == "ffmpeg")
    version = subprocess.run([str(ffmpeg), "-hide_banner", "-version"], capture_output=True,
                             text=True, check=True).stdout
    license_text = subprocess.run([str(ffmpeg), "-hide_banner", "-L"], capture_output=True,
                                  text=True).stdout
    (target / "FFmpeg-build.txt").write_text(
        "Bundled FFmpeg build\n====================\n\n" + version + "\n\n" + license_text,
        encoding="utf-8")


# --------------------------------------------------------------------------- PyInstaller

def pyinstaller(key: str, ffmpeg_dir: Path, licenses: Path, icons: Path) -> Path:
    sep = os.pathsep
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
               "--name", APP, "--distpath", BUILD / "dist", "--workpath", BUILD / "pyi",
               "--specpath", BUILD / "spec", "--collect-data", "peak_alphaloop",
               "--hidden-import", "peak_alphaloop.selftest", "--collect-data", "certifi",
               "--add-data", f"{licenses}{sep}licenses"]
    for module in ("tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PySide6"):
        command += ["--exclude-module", module]
    for item in sorted(ffmpeg_dir.iterdir()):
        command += ["--add-binary", f"{item}{sep}ffmpeg"]
    if key == "windows":
        command += ["--windowed", "--icon", icons / "icon.ico"]
    elif key == "macos":
        command += ["--windowed", "--icon", icons / "icon.icns",
                    "--osx-bundle-identifier", "com.peakprojections.alphaloop"]
    command.append(ROOT / "packaging" / "alphaloop_main.py")
    run(command)
    return BUILD / "dist" / (f"{APP}.app" if key == "macos" else APP)


def finish_macos(app: Path) -> Path:
    plist_path = app / "Contents" / "Info.plist"
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    plist.update({
        "CFBundleName": "Peak Alphaloop", "CFBundleDisplayName": "Peak Alphaloop",
        "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
        # No Dock icon: the app is a local server that lives in the browser tab.
        "LSUIElement": True, "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "© Peak Projections. MIT License.",
    })
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)
    final = app.with_name("Peak Alphaloop.app")
    if final.exists():
        shutil.rmtree(final)
    app.rename(final)
    run(["codesign", "--force", "--deep", "--sign", "-", final])
    return final


def self_test(executable: Path, bundle_root: Path) -> None:
    log("Running the built app's self-test")
    report = BUILD / "selftest.json"
    report.unlink(missing_ok=True)
    result = subprocess.run([str(executable), "--self-test", str(report)], timeout=600)
    data = json.loads(report.read_text()) if report.exists() else {"ok": False, "error": "no report"}
    print(json.dumps(data, indent=2))
    if result.returncode != 0 or not data.get("ok"):
        raise SystemExit("Self-test failed.")
    ffmpeg_used = Path(str(data["checks"]["ffmpeg"])).resolve()
    if bundle_root.resolve() not in ffmpeg_used.parents:
        raise SystemExit(f"Self-test used {ffmpeg_used}, not the bundled FFmpeg.")
    if not data.get("frozen"):
        raise SystemExit("Self-test did not run inside the frozen app.")


def package(key: str, built: Path, icons: Path) -> Path:
    RELEASE.mkdir(exist_ok=True)
    name = f"{APP}-{VERSION}-{ASSET_SUFFIXES[key]}"
    output = RELEASE / name
    output.unlink(missing_ok=True)
    if key == "windows":
        iscc = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe"
        if not iscc.exists():
            run(["choco", "install", "innosetup", "-y", "--no-progress"])
        run([iscc, f"/DAppVersion={VERSION}", f"/DSourceDir={built}", f"/DOutputDir={RELEASE}",
             f"/DOutputName={output.stem}", ROOT / "packaging" / "windows" / "installer.iss"])
    elif key == "macos":
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", built, output])
    else:
        shutil.copy2(icons / "icon.png", built / "peak-alphaloop.png")
        launcher = built / "install-desktop-entry.sh"
        launcher.write_text(LINUX_DESKTOP_SCRIPT, encoding="utf-8")
        launcher.chmod(0o755)
        with tarfile.open(output, "w:gz") as archive:
            archive.add(built, arcname=APP)
    log(f"Built {output} ({output.stat().st_size / 1e6:.1f} MB)")
    return output


LINUX_DESKTOP_SCRIPT = """#!/bin/sh
# Adds Peak Alphaloop to your application menu (run once from this folder).
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/peak-alphaloop.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Peak Alphaloop
Comment=Remove video backgrounds, make seamless loops, basic edits
Exec="$HERE/PeakAlphaloop" %F
Icon=$HERE/peak-alphaloop.png
Terminal=false
Categories=AudioVideo;Video;
MimeType=video/mp4;video/webm;video/quicktime;video/x-matroska;
EOF
echo "Added Peak Alphaloop to your applications menu."
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ffmpeg-dir", type=Path, help="Use FFmpeg from here instead of downloading.")
    parser.add_argument("--gpl-text", type=Path, help="Local copy of the GPL-3.0 text.")
    parser.add_argument("--skip-self-test", action="store_true")
    args = parser.parse_args()

    key = platform_key()
    tag = os.environ.get("GITHUB_REF_NAME", "") if os.environ.get("GITHUB_REF_TYPE") == "tag" else ""
    if tag and tag.lstrip("v") != VERSION:
        raise SystemExit(f"Tag {tag} does not match package version {VERSION}.")
    log(f"Building Peak Alphaloop {VERSION} for {key} ({platform.machine()})")
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir()

    ffmpeg_dir = BUILD / "ffmpeg"
    if args.ffmpeg_dir:
        copy_local_ffmpeg(args.ffmpeg_dir, ffmpeg_dir)
    else:
        fetch_ffmpeg(key, ffmpeg_dir)
    licenses = BUILD / "licenses"
    write_licenses(ffmpeg_dir, licenses, args.gpl_text)
    icons = BUILD / "icon"
    run([sys.executable, ROOT / "packaging" / "make_icon.py", icons])

    built = pyinstaller(key, ffmpeg_dir, licenses, icons)
    if key == "macos":
        built = finish_macos(built)
        executable = built / "Contents" / "MacOS" / APP
    else:
        executable = built / (f"{APP}.exe" if key == "windows" else APP)
    if not args.skip_self_test:
        self_test(executable, built)
    package(key, built, icons)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
