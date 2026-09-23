"""CLI adapters for configuration and themes."""

import os
import re
import shlex
import shutil
import subprocess
from typing import Annotated, Literal, Optional

import typer

from skill_ctl.config import DEFAULT_CONFIG_YAML, ensure_config, load_config, invalidate_config_cache
from skill_ctl.prompts import prompt_theme
from skill_ctl.theme import THEMES
from skill_ctl.ui import console, print_success, print_warn


def config_cmd(action: Annotated[Literal["show", "path", "edit", "reset"], typer.Argument()] = "show") -> None:
    """Manage skill-ctl configuration."""
    path = ensure_config()
    if action == "path":
        print(path)
    elif action == "show":
        print(path.read_text(encoding="utf-8"), end="")
    elif action == "edit":
        editor = os.environ.get("EDITOR") or shutil.which("nano") or shutil.which("vim") or "vi"
        subprocess.run([*shlex.split(editor), str(path)])
        invalidate_config_cache()
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
    action: Annotated[Optional[str], typer.Argument()] = None,
    name: Annotated[Optional[str], typer.Argument()] = None,
) -> None:
    """List or change the color theme. Omit arguments to pick interactively."""
    if action == "list":
        if name:
            print_warn("`skctl theme list` does not take a theme name.")
            raise SystemExit(1)
        print("\n".join(THEMES))
        return
    if action == "set":
        if not name:
            print_warn("Usage: skctl theme set <name>")
            raise SystemExit(1)
    elif name:
        print_warn("Usage: skctl theme [list|set <name>|<name>]")
        raise SystemExit(1)
    else:
        name = action
    current = load_config().get("theme", "default")
    if name is None:
        name = prompt_theme(list(THEMES), current)
    elif name not in THEMES:
        print_warn(f"Unknown theme '{name}'. Available: {', '.join(THEMES)}")
        raise SystemExit(1)
    set_theme_in_config(name)
    print_success(f"Theme set to '{name}'.")
