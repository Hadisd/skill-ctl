"""CLI adapters for configuration and themes."""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal, Optional

import typer

from skill_ctl.config import DEFAULT_CONFIG_YAML, ensure_config, load_config, invalidate_config_cache
from skill_ctl.prompts import prompt_theme
from skill_ctl.theme import THEMES, is_valid_theme, resolve_theme_name
from skill_ctl.ui import console, print_success, print_warn, set_theme


def get_editor_command(path: Path) -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        if os.name == "nt" or sys.platform == "win32":
            args = [arg.strip('"') for arg in shlex.split(editor, posix=False)]
        else:
            args = shlex.split(editor)
        return [*args, str(path)]

    if os.name == "nt" or sys.platform == "win32":
        candidates = ["notepad"]
    else:
        candidates = ["nano", "vim", "vi"]

    for name in candidates:
        found = shutil.which(name)
        if found:
            return [found, str(path)]

    return [candidates[0], str(path)]


def config_cmd(action: Annotated[Literal["show", "path", "edit", "reset"], typer.Argument()] = "show") -> None:
    """Manage skill-ctl configuration."""
    path = ensure_config()
    if action == "path":
        print(path)
    elif action == "show":
        print(path.read_text(encoding="utf-8"), end="")
    elif action == "edit":
        cmd = get_editor_command(path)
        try:
            subprocess.run(cmd)
            invalidate_config_cache()
        except FileNotFoundError:
            print_warn(
                f"Editor '{cmd[0]}' not found. Set the $EDITOR or $VISUAL environment variable to your preferred editor."
            )
            raise SystemExit(1)
    elif action == "reset":
        path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
        invalidate_config_cache()
        print_success(f"Reset configuration to defaults at {path}")


def set_theme_in_config(name: str) -> None:
    path = ensure_config()
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^theme:.*$", f"theme: {name}", text)
    path.write_text(updated if count else text.rstrip("\n") + f"\ntheme: {name}\n", encoding="utf-8")
    invalidate_config_cache()



def theme_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Theme name, 'list', or omit to pick interactively")] = None,
    list_all: Annotated[bool, typer.Option("--list", "-l", help="List all available themes with color previews")] = False,
) -> None:
    """View or change the color theme. Omit the name to pick interactively."""
    current = load_config().get("theme", "default")
    if list_all or (name and name.lower() in ("list", "ls")):
        console.print("[dim]Available themes:[/dim]")
        for theme_name, palette in THEMES.items():
            marker = "[success]*[/success]" if theme_name == current else " "
            swatch = f"[{palette['header']}]■[/] [{palette['accent']}]■[/] [{palette['choice']}]■[/] [{palette['success']}]■[/] [{palette['warn']}]■[/] [{palette['error']}]■[/]"
            console.print(f" {marker} {swatch}  [bold]{theme_name}[/bold]")
        return

    if name is None:
        name = prompt_theme(list(THEMES), current)
    elif not is_valid_theme(name):
        print_warn(f"Unknown theme '{name}'. Available: {', '.join(THEMES)}")
        raise SystemExit(1)
    else:
        name = resolve_theme_name(name)

    set_theme_in_config(name)
    set_theme(name)
    print_success(f"Theme set to '{name}'.")
