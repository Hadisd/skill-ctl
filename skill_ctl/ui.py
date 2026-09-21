"""Generic Rich console formatting."""

import re
from typing import Optional

from rich.console import Console
from rich.markup import escape

from skill_ctl.theme import build_theme

_default_theme = build_theme()
console = Console(theme=_default_theme)
err_console = Console(stderr=True, theme=_default_theme)

_TAG_RE = re.compile(r"\[/?[a-zA-Z_]+\]")


def set_theme(name: Optional[str]) -> None:
    theme = build_theme(name)
    console.push_theme(theme, inherit=False)
    err_console.push_theme(theme, inherit=False)


def _safe_print(target_console: Console, text: str) -> None:
    """Rich itself can fail to render (e.g. a broken/mismatched optional
    dependency such as rich's own unicode-width tables), which must not
    swallow the message a caller is trying to report. Fall back to plain
    text so the actual error always reaches the user."""
    try:
        target_console.print(text)
    except Exception:
        print(_TAG_RE.sub("", text))


def print_header(text: str) -> None:
    _safe_print(console, f"[header]◇[/header]  [bold]{escape(text)}[/bold]")


def print_success(text: str) -> None:
    _safe_print(console, f"[success]✓[/success]  {escape(text)}")


def print_warn(text: str) -> None:
    _safe_print(console, f"[warn]![/warn]  {escape(text)}")


def print_error(text: str) -> None:
    _safe_print(err_console, f"[error]✗[/error]  {escape(text)}")


# Backward-compatible re-exports
from rich.prompt import Prompt
from skill_ctl.constants import ALL_PRESETS
from skill_ctl.prompts import (
    prompt_add_source,
    prompt_apply_type,
    prompt_destination,
    prompt_new_preset_name,
    prompt_preset,
    prompt_preset_or_new,
    prompt_skills,
    prompt_theme,
)

