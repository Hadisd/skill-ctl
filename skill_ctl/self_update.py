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
from skill_ctl.ui import print_error, print_success

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


def installer_command(prefix: Path, executable: Path, wheel_url: str) -> list[str]:
    prefix_text = str(prefix).replace("\\", "/").lower()
    if "/uv/tools/skill-ctl" in prefix_text:
        return ["uv", "tool", "install", "--reinstall", wheel_url]
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

    command = installer_command(Path(sys.prefix), Path(sys.executable), release.wheel_url)
    if os.name == "nt":
        run_installer_after_exit_windows(command, release.tag)
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


def run_installer_after_exit_windows(command: list[str], tag: str) -> None:
    """Windows keeps this process's own install directory (the `Scripts`
    folder holding skctl.exe) locked while it runs, so the installer cannot
    delete and replace it from inside this same process - `uv tool install
    --reinstall` fails with "Access is denied" removing that folder.

    Hand the install off to a detached process that waits for this one to
    exit, then run the installer; exit immediately ourselves so the lock is
    released before it gets there.
    """
    pid = os.getpid()
    quoted = " ".join(f'"{part}"' if " " in part else part for part in command)
    ps_command = f"Wait-Process -Id {pid} -ErrorAction SilentlyContinue; {quoted}"
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_command],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as error:
        print_error(f"Could not start installer: {error}")
        raise SystemExit(1) from error
    print_success(
        f"skctl will finish updating to {tag} in the background once this exits "
        "(Windows can't replace its own running files) - re-run `skctl --version` in a few seconds to confirm."
    )
