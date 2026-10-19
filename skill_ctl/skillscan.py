"""Backward-compatibility proxy for skill_ctl.core.skillscan."""

import sys

from skill_ctl.core import skillscan as _skillscan
from skill_ctl.core.skillscan import *  # noqa: F403

sys.modules[__name__] = _skillscan
