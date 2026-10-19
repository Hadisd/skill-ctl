"""Backward-compatibility proxy for skill_ctl.commands.diff."""

import sys

from skill_ctl.commands import diff as _diff
from skill_ctl.commands.diff import *  # noqa: F403

sys.modules[__name__] = _diff
