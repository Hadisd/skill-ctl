"""Backward-compatibility proxy for skill_ctl.core.archive."""

import sys

from skill_ctl.core import archive as _archive
from skill_ctl.core.archive import *  # noqa: F403

sys.modules[__name__] = _archive
