"""Backward-compatibility proxy for skill_ctl.commands.backup."""

import sys

from skill_ctl.commands import backup as _backup
from skill_ctl.commands.backup import *  # noqa: F403

sys.modules[__name__] = _backup
