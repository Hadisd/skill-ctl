"""Backward-compatibility proxy for skill_ctl.core.constants."""

import sys

from skill_ctl.core import constants as _constants
from skill_ctl.core.constants import *  # noqa: F403

sys.modules[__name__] = _constants
