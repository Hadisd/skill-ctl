"""Generic Rich console formatting."""

from typing import Optional

from rich.console import Console
from rich.markup import escape

from skill_ctl.theme import build_theme

_default_theme = build_theme()
console = Console(theme=_default_theme)
err_console = Console(stderr=True, theme=_default_theme)


def set_theme(name: Optional[str]) -> None:
    theme = build_theme(name)
    console.push_theme(theme, inherit=False)
    err_console.push_theme(theme, inherit=False)


def print_header(text: str) -> None:
    console.print(f"[header]◇[/header]  [bold]{escape(text)}[/bold]")


def print_success(text: str) -> None:
    console.print(f"[success]✓[/success]  {escape(text)}")


def print_warn(text: str) -> None:
    console.print(f"[warn]![/warn]  {escape(text)}")


def print_error(text: str) -> None:
    err_console.print(f"[error]✗[/error]  {escape(text)}")


# Backward-compatible re-exports
from rich.prompt import Prompt
from skill_ctl.constants import ALL_PRESETS
from skill_ctl.prompts import (
    prompt_add_source,
    prompt_destination,
    prompt_new_preset_name,
    prompt_preset,
    prompt_preset_or_new,
    prompt_skills,
    prompt_theme,
)

