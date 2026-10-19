"""Backward-compatibility proxy for skill_ctl.commands.presets."""

import sys

from skill_ctl.commands import presets as _presets
from skill_ctl.commands.presets import *  # noqa: F403

sys.modules[__name__] = _presets
