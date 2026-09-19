from dataclasses import dataclass
import json
import os
from pathlib import Path
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

    command = installer_command(Path(sys.prefix), Path(sys.executable), release.wheel_url, release.tag)
    if os.name == "nt":
        print_windows_manual_command(command)
        return
    try:
        result = subprocess.run(command)
    except OSError as error:
        print_error(f"Could not start installer {command[0]}: {error}")
        raise SystemExit(1) from error
    if result.returncode != 0:
        print_error(f"Could not update skctl to {release.tag}.")
        raise SystemExit(1)
    print_success(f"Updated skctl to {release.tag}.")


def print_windows_manual_command(command: list[str]) -> None:
    """Windows keeps this process's own install directory locked while it
    runs, so skctl cannot replace itself from inside its own process.

    A detached background process was tried here before, to run the
    installer once this process exits; it was unreliable in practice (silent
    failures, timing-dependent) and harder to debug than just running the
    command directly. Printing it for a manual run in a fresh shell matches
    what's actually proven to work.
    """
    quoted = " ".join(f'"{part}"' if " " in part else part for part in command)
    print_warn("Windows can't replace its own running files while skctl is running them.")
    console.print("Run this in a new terminal (after closing this one):")
    console.print(f"  [header]{quoted}[/header]")
