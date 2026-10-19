"""Backward-compatibility proxy for skill_ctl.core.runner."""

import sys

from skill_ctl.core import runner as _runner
from skill_ctl.core.runner import *  # noqa: F403

sys.modules[__name__] = _runner
