"""Backward-compatibility proxy for skill_ctl.commands.config_commands."""

import sys

from skill_ctl.commands import config_commands as _config_commands
from skill_ctl.commands.config_commands import *  # noqa: F403

sys.modules[__name__] = _config_commands
