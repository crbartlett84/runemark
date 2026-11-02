"""Updates from GitHub releases: check, download, verify, stage, and swap in when Runemark closes.

1. **Check**: the latest release on github.com/crbartlett84/runemark, compared with this version.
2. **Download** the `Runemark-x.y.z-win64.zip` asset and check its SHA-256 against the digest GitHub publishes
   for it. No digest, or a mismatch, and nothing is installed.
3. **Stage**: unzip beside the app folder (`Runemark-update`), and check it holds `Runemark.exe`.
4. **Swap**, after Runemark closes, by a small PowerShell script: the app folder becomes `Runemark-previous`
   (the fallback), the staged folder takes its place, a portable data folder moves across, and the new version
   starts. If anything fails, the old folder is put back and the old version starts.

Your data (tag database, settings, backups) lives in %APPDATA%\\Runemark and isn't touched; in portable mode the
data folder beside the app is moved into the new version, never copied or replaced.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .paths import FROZEN, PORTABLE_FOLDER, PORTABLE_MARKER, app_folder

log = logging.getLogger(__name__)

REPO = "crbartlett84/runemark"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
ASSET_PATTERN = re.compile(r"^Runemark-(\d+\.\d+\.\d+)-win64\.zip$")
STAGE_NAME = "Runemark-update"
PREVIOUS_NAME = "Runemark-previous"
KEEP = (PORTABLE_MARKER, PORTABLE_FOLDER)        # carried from the old folder into the new one
TIMEOUT = 30


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    asset_name: str
    url: str
    sha256: str | None
    size: int
    page: str

    @property
    def newer(self) -> bool:
        return parse_version(self.version) > parse_version(__version__)


def parse_version(text: str) -> tuple[int, ...]:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"Runemark/{__version__}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (fixed https URL)
        return resp.read()


def parse_release(data: dict) -> Release | None:
    """The release's Windows zip, or None when it has none."""
    for asset in data.get("assets") or []:
        m = ASSET_PATTERN.match(asset.get("name", ""))
        if m:
            digest = asset.get("digest") or ""
            sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None
            return Release(m.group(1), data.get("tag_name", ""), data.get("body") or "", asset["name"],
                           asset.get("browser_download_url", ""), sha, int(asset.get("size") or 0),
                           data.get("html_url") or RELEASES_PAGE)
    return None


def check(fetch: Callable[[str], bytes] = _get) -> Release | None:
    """The latest release (whether or not it's newer), or None when there's no Windows build in it."""
    return parse_release(json.loads(fetch(LATEST_URL).decode("utf-8")))


def can_install() -> tuple[bool, str]:
    """Whether this copy can update itself (the packaged app, in a folder it may write beside)."""
    if not FROZEN:
        return False, "Runemark is running from its source code here: update it with git instead."
    parent = app_folder().parent
    try:
        probe = tempfile.NamedTemporaryFile(dir=parent, prefix=".runemark-write-test-", delete=True)
        probe.close()
    except OSError:
        return False, (f"Runemark can't write to {parent}, so it can't update itself there. Download the new "
                       "version from the releases page instead, or move Runemark to a folder you own.")
    return True, ""


def download(release: Release, dest: Path, progress: Callable[[int, str], None] | None = None,
             cancel=None) -> Path:
    """Download the zip to dest and check its SHA-256. Raises UpdateError when it can't be trusted."""
    if not release.sha256:
        raise UpdateError("GitHub didn't publish a checksum for this download, so Runemark won't install it. "
                          "Download it from the releases page instead.")
    if not release.url.startswith("https://github.com/" + REPO + "/releases/download/"):
        raise UpdateError("The download isn't on Runemark's own releases page.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    req = urllib.request.Request(release.url, headers={"User-Agent": f"Runemark/{__version__}"})
    done = 0
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp, open(dest, "wb") as out:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or release.size or 0)
        while True:
            if cancel is not None and cancel.is_set():
                raise UpdateError("Cancelled.")
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(int(done * 100 / total), f"Downloading {release.asset_name}…")
    if digest.hexdigest().lower() != release.sha256:
        dest.unlink(missing_ok=True)
        raise UpdateError("The download doesn't match the checksum GitHub published for it, so it was thrown "
                          "away. Try again later.")
    return dest


def stage(zip_path: Path, app_dir: Path | None = None) -> Path:
    """Unzip next to the app folder and check it's a whole Runemark. Returns the staged folder."""
    app_dir = app_dir or app_folder()
    target = app_dir.parent / STAGE_NAME
    if target.exists():
        shutil.rmtree(target)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():                      # nothing may land outside the folder
            resolved = (target / name).resolve()
            if not resolved.is_relative_to(target.resolve()):
                raise UpdateError("The download contains an unsafe path; not installing it.")
        zf.extractall(target)
    exe = next(target.rglob("Runemark.exe"), None)
    if exe is None:
        shutil.rmtree(target, ignore_errors=True)
        raise UpdateError("The download doesn't contain Runemark.exe.")
    if exe.parent != target:                            # a zip with a top folder: use that folder
        inner = exe.parent
        moved = target.with_name(STAGE_NAME + "-inner")
        shutil.move(str(inner), str(moved))
        shutil.rmtree(target)
        moved.rename(target)
    return target


def swap_script(app_dir: Path, staged: Path, pid: int, log_path: Path) -> str:
    """The PowerShell that swaps the folders once Runemark (pid) has closed, rolling back on failure."""
    def q(p) -> str:
        return "'" + str(p).replace("'", "''") + "'"
    previous = app_dir.parent / PREVIOUS_NAME
    keep = ", ".join(q(k) for k in KEEP)
    return f"""$ErrorActionPreference = 'Stop'
$app = {q(app_dir)}; $new = {q(staged)}; $old = {q(previous)}; $log = {q(log_path)}
function Note($t) {{ Add-Content -Path $log -Value ("{{0}}  {{1}}" -f (Get-Date -Format s), $t) }}
Note "Waiting for Runemark (pid {pid}) to close"
try {{ Wait-Process -Id {pid} -Timeout 120 -ErrorAction SilentlyContinue }} catch {{}}
Start-Sleep -Milliseconds 800
$swapped = $false
try {{
    if (Test-Path $old) {{ Remove-Item -LiteralPath $old -Recurse -Force }}
    Rename-Item -LiteralPath $app -NewName (Split-Path $old -Leaf)
    try {{
        Rename-Item -LiteralPath $new -NewName (Split-Path $app -Leaf)
        $swapped = $true
    }} catch {{
        Rename-Item -LiteralPath $old -NewName (Split-Path $app -Leaf)
        throw
    }}
    foreach ($k in @({keep})) {{
        $from = Join-Path $old $k
        if (Test-Path -LiteralPath $from) {{ Move-Item -LiteralPath $from -Destination (Join-Path $app $k) -Force }}
    }}
    Note "Updated: the previous version is kept in $old"
    Start-Process -FilePath (Join-Path $app 'Runemark.exe') -ArgumentList '--updated'
}} catch {{
    Note ("Update failed: " + $_.Exception.Message)
    if ($swapped) {{
        Rename-Item -LiteralPath $app -NewName (Split-Path $new -Leaf) -ErrorAction SilentlyContinue
        Rename-Item -LiteralPath $old -NewName (Split-Path $app -Leaf) -ErrorAction SilentlyContinue
    }}
    Start-Process -FilePath (Join-Path $app 'Runemark.exe') -ArgumentList '--update-failed'
}}
"""


def launch_swap(staged: Path) -> Path:
    """Write the swap script and start it hidden; Runemark must quit straight after. Returns the log path."""
    import subprocess
    tmp = Path(tempfile.gettempdir())
    script = tmp / "runemark-update.ps1"
    log_path = tmp / "runemark-update.log"
    script.write_text(swap_script(app_folder(), staged, os.getpid(), log_path), encoding="utf-8-sig")
    flags = 0x08000000 | 0x00000200      # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: outlives Runemark
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(script)], creationflags=flags, close_fds=True)
    return log_path
