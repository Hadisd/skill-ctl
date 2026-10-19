"""Terminal UI presentation layer for skill-ctl."""

from skill_ctl.ui.console import (
    console,
    err_console,
    print_error,
    print_header,
    print_success,
    print_warn,
    set_theme,
)
from skill_ctl.ui.theme import THEMES, build_theme, is_valid_theme, resolve_theme_name

__all__ = [
    "THEMES",
    "build_theme",
    "console",
    "err_console",
    "is_valid_theme",
    "print_error",
    "print_header",
    "print_success",
    "print_warn",
    "resolve_theme_name",
    "set_theme",
]

_LAZY_EXPORTS = {
    "ALL_PRESETS": ("skill_ctl.constants", "ALL_PRESETS"),
    "Prompt": ("rich.prompt", "Prompt"),
    "prompt_add_source": ("skill_ctl.ui.prompts", "prompt_add_source"),
    "prompt_apply_destination": ("skill_ctl.ui.prompts", "prompt_apply_destination"),
    "prompt_apply_type": ("skill_ctl.ui.prompts", "prompt_apply_type"),
    "prompt_destination": ("skill_ctl.ui.prompts", "prompt_destination"),
    "prompt_new_preset_name": ("skill_ctl.ui.prompts", "prompt_new_preset_name"),
    "prompt_preset": ("skill_ctl.ui.prompts", "prompt_preset"),
    "prompt_preset_or_new": ("skill_ctl.ui.prompts", "prompt_preset_or_new"),
    "prompt_skills": ("skill_ctl.ui.prompts", "prompt_skills"),
    "prompt_theme": ("skill_ctl.ui.prompts", "prompt_theme"),
}


def __getattr__(name: str):
    if name in _LAZY_EXPORTS:
        mod_name, attr_name = _LAZY_EXPORTS[name]
        mod = __import__(mod_name, fromlist=[attr_name])
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
