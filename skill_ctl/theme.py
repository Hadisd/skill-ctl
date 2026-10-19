"""Backward-compatibility proxy for skill_ctl.ui.theme."""

import sys

from skill_ctl.ui import theme as _theme
from skill_ctl.ui.theme import *  # noqa: F403
from skill_ctl.ui.theme import (  # noqa: F401
    THEMES,
    bat_theme,
    build_theme,
    fzf_color_arg,
    install_typer_styling,
    is_valid_theme,
    picker_ansi,
    resolve_theme_name,
)

sys.modules[__name__] = _theme

