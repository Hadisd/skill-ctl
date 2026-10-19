"""Backward-compatibility proxy for skill_ctl.commands.self_update."""

import sys

from skill_ctl.commands import self_update as _self_update
from skill_ctl.commands.self_update import *  # noqa: F403

sys.modules[__name__] = _self_update
