"""Color themes for skctl's terminal output.

Every place that used to hardcode a rich color (``[bold cyan]``, ``style="green"``)
now names a semantic style instead (``[header]``, ``style="path"``). This module
maps those semantic names to real colors, once per theme, so switching themes
never means touching the command files again.
"""

import os
from typing import Optional

from rich.color import ColorSystem, ColorType
from rich.style import Style
from rich.theme import Theme

# Semantic roles used throughout the codebase:
#   header   - section titles, panel borders, the "0) all ..." leading entry
#   accent   - bare emphasis in the same hue as header (e.g. destination paths)
#   choice   - numbered list items / table "key" columns
#   success  - confirmations
#   warn     - non-fatal warnings
#   error    - failures (printed to stderr)
#   path     - filesystem paths shown for context

THEMES: dict[str, dict[str, str]] = {
    "default": {
        "header": "bold cyan",
        "accent": "cyan",
        "choice": "bold blue",
        "success": "bold green",
        "warn": "bold yellow",
        "error": "bold red",
        "path": "green",
    },
    "dark": {
        "header": "bold bright_magenta",
        "accent": "bright_magenta",
        "choice": "bold bright_cyan",
        "success": "bold bright_green",
        "warn": "bold bright_yellow",
        "error": "bold bright_red",
        "path": "bright_green",
    },
    "light": {
        "header": "bold blue",
        "accent": "blue",
        "choice": "bold cyan",
        "success": "bold dark_green",
        "warn": "bold dark_orange",
        "error": "bold dark_red",
        "path": "dark_green",
    },
    "mono": {
        "header": "bold",
        "accent": "bold",
        "choice": "bold",
        "success": "bold",
        "warn": "bold underline",
        "error": "bold underline",
        "path": "italic",
    },
    "catppuccin-mocha": {
        "header": "bold #cba6f7",   # mauve
        "accent": "#89b4fa",        # blue
        "choice": "bold #74c7ec",   # sapphire
        "success": "bold #a6e3a1",  # green
        "warn": "bold #f9e2af",     # yellow
        "error": "bold #f38ba8",    # red
        "path": "#94e2d5",          # teal
    },
    "tokyo-night": {
        "header": "bold #bb9af7",   # purple
        "accent": "#7aa2f7",        # blue
        "choice": "bold #7dcfff",   # cyan
        "success": "bold #9ece6a",  # green
        "warn": "bold #e0af68",     # yellow
        "error": "bold #f7768e",    # red
        "path": "#73daca",         # teal
    },
    "gruvbox": {
        "header": "bold #d3869b",   # purple
        "accent": "#83a598",        # blue
        "choice": "bold #8ec07c",   # aqua
        "success": "bold #b8bb26",  # green
        "warn": "bold #fabd2f",     # yellow
        "error": "bold #fb4934",    # red
        "path": "#fe8019",          # orange
    },
}

DEFAULT_THEME = "default"


def theme_names() -> list[str]:
    return list(THEMES)


def resolve_theme_name(name: Optional[str]) -> str:
    """The theme to actually use: NO_COLOR wins, then an unknown name falls back."""
    if os.environ.get("NO_COLOR"):
        return "mono"
    if name in THEMES:
        return name
    return DEFAULT_THEME


def build_theme(name: Optional[str] = None) -> Theme:
    return Theme(THEMES[resolve_theme_name(name)])


def style_typer_help(name: Optional[str] = None) -> None:
    """Point typer's help renderer at the active theme.

    Typer builds its own Console from module-level style constants, so the
    theme on ``ui.console`` cannot reach it. Rewriting the constants before a
    command runs is what keeps `skctl apply --help`, which typer renders, in
    the same colors as the help cli.py draws by hand. The constants are read
    at render time, so this only has to happen once per process.

    Styles have to be concrete colors, not the semantic names: typer's console
    has its own theme and would not resolve ``header``.
    """
    from typer import rich_utils

    palette = THEMES[resolve_theme_name(name)]
    rich_utils.STYLE_OPTIONS_PANEL_BORDER = palette["header"]
    rich_utils.STYLE_COMMANDS_PANEL_BORDER = palette["header"]
    rich_utils.STYLE_ERRORS_PANEL_BORDER = palette["error"]
    rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN = palette["header"]
    rich_utils.STYLE_OPTION = palette["choice"]
    rich_utils.STYLE_SWITCH = palette["choice"]
    rich_utils.STYLE_TYPES = palette["accent"]
    rich_utils.STYLE_USAGE = palette["accent"]
    rich_utils.STYLE_REQUIRED_SHORT = palette["error"]
    rich_utils.STYLE_REQUIRED_LONG = palette["error"]


def install_typer_styling(name: Optional[str] = None) -> None:
    """Run style_typer_help() the moment typer imports its rich renderer.

    Typer only imports typer.rich_utils when it renders help or an error, and
    that import drags in rich.markdown, markdown_it and pygments (~25ms). A
    command that simply runs never needs any of it, so importing the module
    up front just to restyle it charged every invocation for a renderer it
    never used. This waits for typer's own import instead, then restyles.
    """
    import importlib.abc
    import importlib.util
    import sys

    if "typer.rich_utils" in sys.modules:
        style_typer_help(name)
        return

    class RestyleOnImport(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname != "typer.rich_utils":
                return None
            # Step aside first, so the lookup below reaches the real finders.
            sys.meta_path.remove(self)
            spec = importlib.util.find_spec(fullname)
            if spec is None or spec.loader is None:
                return None
            load = spec.loader.exec_module

            def exec_module(module):
                load(module)
                style_typer_help(name)

            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, RestyleOnImport())


ANSI_RESET = "\x1b[0m"
_MARKER = "\x00"


def _ansi_prefix(style: str) -> str:
    """Raw ANSI escape codes for a style string, with no text or reset attached.

    fzf's --ansi rows are built by concatenating raw escape codes with plain
    text, not through a Console, so this pulls just the codes out of what
    rich would otherwise wrap around a string.
    """
    rendered = Style.parse(style).render(_MARKER, color_system=ColorSystem.TRUECOLOR)
    return rendered.split(_MARKER)[0]


# bat ships these built in, so no need to check `bat --list-themes` first.
# Nord matches the default cyan/blue/green palette; tokyo-night also maps to Nord.
BAT_THEMES: dict[str, str] = {
    "default": "Nord",
    "dark": "Dracula",
    "light": "GitHub",
    "mono": "ansi",
    "catppuccin-mocha": "Catppuccin Mocha",
    "tokyo-night": "Nord",
    "gruvbox": "gruvbox-dark",
}


def bat_theme(name: Optional[str] = None) -> str:
    """The bat --theme value matching this skctl theme, for the SKILL.md preview."""
    return BAT_THEMES[resolve_theme_name(name)]


def picker_ansi(name: Optional[str] = None) -> dict[str, str]:
    """ANSI prefixes for fzf row columns (skill name / location / updated / description),
    matching the roles used for other output so the picker isn't the one place
    a theme doesn't reach."""
    palette = THEMES[resolve_theme_name(name)]
    return {
        "name": _ansi_prefix(palette["header"]),
        "location": _ansi_prefix(f"dim {palette['accent']}"),
        "updated": _ansi_prefix("dim"),
        "description": _ansi_prefix("dim"),
        "reset": ANSI_RESET,
    }


def _fzf_value(style: str) -> str:
    """A style's color as fzf wants it: the terminal's own ANSI slot number for
    a named color (0-255), so it renders in whatever palette the user's
    terminal defines, and #rrggbb only for the true-color hex palettes - a
    fixed hex for "cyan" would flatten it to rich's own approximation instead
    of the terminal's actual (often brighter) rendering.
    """
    color = Style.parse(style).color
    if color.type == ColorType.TRUECOLOR:
        triplet = color.get_truecolor()
        return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"
    return str(color.number)


def fzf_color_arg(name: Optional[str] = None) -> str:
    """The value for fzf's own `--color`, so its chrome (prompt, pointer,
    header, match highlight) follows the theme too, not just the rows and
    preview pane skctl builds itself. 'bw' for mono matches NO_COLOR intent.

    bg/bg+ are left unset on purpose, so fzf keeps the terminal's own
    background instead of painting over it.
    """
    resolved = resolve_theme_name(name)
    if resolved == "mono":
        return "bw"
    palette = THEMES[resolved]
    parts = {
        "prompt": _fzf_value(palette["choice"]),
        "pointer": _fzf_value(palette["success"]),
        "marker": _fzf_value(palette["success"]),
        "spinner": _fzf_value(palette["accent"]),
        "info": _fzf_value(palette["accent"]),
        "header": f"{_fzf_value(palette['header'])}:dim",
        "hl": _fzf_value(palette["accent"]),
        "hl+": _fzf_value(palette["header"]),
    }
    return ",".join(f"{k}:{v}" for k, v in parts.items())
