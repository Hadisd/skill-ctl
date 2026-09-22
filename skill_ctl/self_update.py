from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Annotated
from urllib.error import URLError
from urllib.request import Request, urlopen

import typer
from rich.prompt import Confirm

from skill_ctl import __version__
from skill_ctl.ui import console, print_error, print_success, print_warn

LATEST_RELEASE_URL = "https://api.github.com/repos/Hadisd/skill-ctl/releases/latest"


@dataclass(frozen=True)
class Release:
    version: tuple[int, int, int]
    tag: str
    wheel_url: str


class SelfUpdateError(ValueError):
    pass


def parse_version(value: str) -> tuple[int, int, int]:
    text = value.removeprefix("v")
    parts = text.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise SelfUpdateError(f"Invalid release version: {value}")
    return tuple(int(part) for part in parts)


def parse_release(payload: object) -> Release:
    if not isinstance(payload, dict):
        raise SelfUpdateError("Invalid release response: expected an object.")
    tag = str(payload.get("tag_name", ""))
    if not tag.startswith("v"):
        raise SelfUpdateError(f"Invalid release tag: {tag}")
    version = parse_version(tag)
    filename = f"skill_ctl-{'.'.join(map(str, version))}-py3-none-any.whl"
    assets = payload.get("assets", [])
    if not isinstance(assets, list):
        raise SelfUpdateError("Invalid release assets: expected a list.")
    for asset in assets:
        if not isinstance(asset, dict):
            raise SelfUpdateError("Invalid release asset: expected an object.")
        if asset.get("name") == filename and asset.get("browser_download_url"):
            return Release(version, tag, str(asset["browser_download_url"]))
    raise SelfUpdateError(f"Release {tag or '<unknown>'} has no matching wheel asset.")


def fetch_latest_release() -> Release:
    request = Request(LATEST_RELEASE_URL, headers={"User-Agent": "skill-ctl"})
    try:
        with urlopen(request, timeout=10) as response:
            return parse_release(json.loads(response.read()))
    except (OSError, URLError, ValueError) as error:
        raise SelfUpdateError(f"Could not check for updates: {error}") from error


def installer_command(prefix: Path, executable: Path, wheel_url: str, tag: str) -> list[str]:
    prefix_text = str(prefix).replace("\\", "/").lower()
    if "/uv/tools/skill-ctl" in prefix_text:
        # A source URL pinned to the release tag, not the release wheel: `uv
        # tool install --reinstall <wheel_url>` forces a full from-scratch
        # reinstall, which on Windows means deleting the whole tool
        # directory (including the running skctl.exe's own Scripts folder)
        # before recreating it - a much heavier, lock-prone operation than
        # the in-place upgrade a plain `uv tool install <source>` performs.
        return ["uv", "tool", "install", f"git+https://github.com/Hadisd/skill-ctl@{tag}"]
    if "/pipx/venvs/skill-ctl" in prefix_text:
        return ["pipx", "install", "--force", wheel_url]
    return [str(executable), "-m", "pip", "install", "--upgrade", wheel_url]


def installed_version() -> tuple[int, int, int]:
    return parse_version(__version__)


def is_windows() -> bool:
    return os.name == "nt"


def cleanup_old_executables() -> None:
    """Silently delete any leftover *.old.exe files from previous Windows self-updates."""
    if not is_windows():
        return
    try:
        prefix = Path(sys.prefix)
        search_dirs = [prefix / "Scripts", prefix / "bin", Path(sys.argv[0]).parent]
        for d in set(search_dirs):
            if d.is_dir():
                for old_file in d.glob("*old.exe"):
                    try:
                        old_file.unlink()
                    except OSError:
                        pass
    except Exception:
        pass


def run_installer_with_windows_handling(command: list[str]) -> int:
    """Execute installer command, swapping running executables on Windows to avoid file locks."""
    renamed: list[tuple[Path, Path]] = []
    if is_windows():
        prefix = Path(sys.prefix)
        candidates = [
            Path(sys.argv[0]),
            prefix / "Scripts" / "skctl.exe",
            prefix / "Scripts" / "skill-ctl.exe",
            prefix / "bin" / "skctl.exe",
            prefix / "bin" / "skill-ctl.exe",
        ]
        for name in ("skctl", "skill-ctl"):
            which_p = shutil.which(name)
            if which_p:
                candidates.append(Path(which_p))

        for exe in set(candidates):
            if exe and exe.is_file() and exe.suffix.lower() == ".exe":
                old_exe = exe.with_name(f"{exe.stem}.old.exe")
                if old_exe.exists():
                    try:
                        old_exe.unlink()
                    except OSError:
                        pass
                try:
                    os.rename(exe, old_exe)
                    renamed.append((exe, old_exe))
                except OSError:
                    pass

    try:
        result = subprocess.run(command)
        returncode = result.returncode
    except OSError as error:
        for orig, old in renamed:
            if not orig.exists() and old.exists():
                try:
                    os.rename(old, orig)
                except OSError:
                    pass
        raise error

    if returncode != 0:
        for orig, old in renamed:
            if not orig.exists() and old.exists():
                try:
                    os.rename(old, orig)
                except OSError:
                    pass
    else:
        for _orig, old in renamed:
            if old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass

    return returncode


def self_update(
    check: Annotated[
        bool,
        typer.Option("--check", help="Report whether an update is available without installing it.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Check for a newer skctl release and install it.

    The installer matches how skctl was installed: uv tool, pipx, or pip.
    """
    try:
        release = fetch_latest_release()
        current_version = installed_version()
    except SelfUpdateError as error:
        print_error(str(error))
        raise SystemExit(1) from error

    current = ".".join(map(str, current_version))
    print(f"Current version: v{current}")
    print(f"Latest version: {release.tag}")

    if current_version >= release.version:
        print("skctl is up to date.")
        return
    if check:
        print("Update available.")
        return
    if not yes and not Confirm.ask(f"Update from v{current} to {release.tag}?", default=False):
        print("Update cancelled.")
        return

    cleanup_old_executables()
    command = installer_command(Path(sys.prefix), Path(sys.executable), release.wheel_url, release.tag)
    try:
        returncode = run_installer_with_windows_handling(command)
    except OSError as error:
        print_error(f"Could not start installer {command[0]}: {error}")
        raise SystemExit(1) from error
    if returncode != 0:
        print_error(f"Could not update skctl to {release.tag}.")
        raise SystemExit(1)
    print_success(f"Updated skctl to {release.tag}.")
