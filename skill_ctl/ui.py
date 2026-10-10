"""Generic Rich console formatting."""

import re
import sys
from typing import Optional

from rich.console import Console
from rich.markup import escape

from skill_ctl.theme import build_theme

class _LazyConsole:
    def __init__(self, stderr: bool = False):
        self._stderr = stderr
        self._instance: Optional[Console] = None

    def _get(self) -> Console:
        if self._instance is None:
            self._instance = Console(stderr=self._stderr, theme=build_theme(_current_theme_name))
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._get(), name)

    # Python looks up context-manager methods on the class, bypassing __getattr__.
    def __enter__(self) -> Console:
        return self._get().__enter__()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return self._get().__exit__(exc_type, exc_value, traceback)


_current_theme_name: Optional[str] = None
console = _LazyConsole(stderr=False)
err_console = _LazyConsole(stderr=True)

_TAG_RE = re.compile(r"\[/?[a-zA-Z_]+\]")


def set_theme(name: Optional[str]) -> None:
    global _current_theme_name
    _current_theme_name = name
    theme = build_theme(name)
    if console._instance is not None:
        console._instance.push_theme(theme, inherit=False)
    if err_console._instance is not None:
        err_console._instance.push_theme(theme, inherit=False)


def _safe_print(target_console: _LazyConsole, text: str) -> None:
    """Rich itself can fail to render (e.g. a broken/mismatched optional
    dependency such as rich's own unicode-width tables, or legacy Windows
    codepages), which must not swallow the message a caller is trying to report.
    Fall back to plain text so the actual error always reaches the user."""
    try:
        target_console.print(text)
    except Exception:
        clean_text = _TAG_RE.sub("", text)
        out = sys.stderr if target_console._stderr else sys.stdout
        try:
            print(clean_text, file=out)
        except UnicodeEncodeError:
            clean_text = clean_text.replace("◇", ">").replace("✓", "+").replace("✗", "x")
            try:
                print(clean_text, file=out)
            except Exception:
                out.write(clean_text.encode("ascii", errors="replace").decode("ascii") + "\n")


def print_header(text: str) -> None:
    _safe_print(console, f"[header]◇[/header]  [bold]{escape(text)}[/bold]")


def print_success(text: str) -> None:
    _safe_print(console, f"[success]✓[/success]  {escape(text)}")


def print_warn(text: str) -> None:
    _safe_print(console, f"[warn]![/warn]  {escape(text)}")


def print_error(text: str) -> None:
    _safe_print(err_console, f"[error]✗[/error]  {escape(text)}")


# Backward-compatible lazy re-exports
from skill_ctl.constants import ALL_PRESETS

_PROMPT_SYMBOLS = {
    "Prompt",
    "prompt_add_source",
    "prompt_apply_type",
    "prompt_destination",
    "prompt_new_preset_name",
    "prompt_preset",
    "prompt_preset_or_new",
    "prompt_skills",
    "prompt_theme",
}


def __getattr__(name: str):
    if name == "Prompt":
        from rich.prompt import Prompt
        return Prompt
    if name in _PROMPT_SYMBOLS:
        import skill_ctl.prompts as _prompts
        return getattr(_prompts, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

