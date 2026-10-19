"""Backward-compatibility proxy for skill_ctl.commands.preset_remote."""

import sys

from skill_ctl.commands import preset_remote as _preset_remote
from skill_ctl.commands.preset_remote import *  # noqa: F403

sys.modules[__name__] = _preset_remote
