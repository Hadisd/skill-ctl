"""Backward-compatibility proxy for skill_ctl.ui.prompts."""

import sys

from skill_ctl.ui import prompts as _prompts
from skill_ctl.ui.prompts import *  # noqa: F403
from skill_ctl.ui.prompts import (  # noqa: F401
    ALL_SKILLS,
    prompt_add_source,
    prompt_apply_destination,
    prompt_apply_type,
    prompt_destination,
    prompt_new_preset_name,
    prompt_preset,
    prompt_preset_or_new,
    prompt_skills,
    prompt_theme,
)

sys.modules[__name__] = _prompts

