"""Backward-compatibility proxy for skill_ctl.commands.completion."""

import sys

from skill_ctl.commands import completion as _completion
from skill_ctl.commands.completion import *  # noqa: F403

sys.modules[__name__] = _completion
