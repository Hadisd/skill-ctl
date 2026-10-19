"""Backward-compatibility proxy for skill_ctl.core.config."""

import sys

from skill_ctl.core import config as _config
from skill_ctl.core.config import *  # noqa: F403

sys.modules[__name__] = _config
