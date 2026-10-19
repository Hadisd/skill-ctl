"""Backward-compatibility proxy for skill_ctl.commands.skills."""

import sys

from skill_ctl.commands import skills as _skills
from skill_ctl.commands.skills import *  # noqa: F403

sys.modules[__name__] = _skills
