"""Backward-compatibility proxy for skill_ctl.commands.doctor."""

import sys

from skill_ctl.commands import doctor as _doctor
from skill_ctl.commands.doctor import *  # noqa: F403

sys.modules[__name__] = _doctor
