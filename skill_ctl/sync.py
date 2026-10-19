"""Backward-compatibility proxy for skill_ctl.commands.sync."""

import sys

from skill_ctl.commands import sync as _sync
from skill_ctl.commands.sync import *  # noqa: F403

sys.modules[__name__] = _sync
