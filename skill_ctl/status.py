"""Backward-compatibility proxy for skill_ctl.commands.status."""

import sys

from skill_ctl.commands import status as _status
from skill_ctl.commands.status import *  # noqa: F403

sys.modules[__name__] = _status
