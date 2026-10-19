"""Backward-compatibility proxy for skill_ctl.core.scanner."""

import sys

from skill_ctl.core import scanner as _scanner
from skill_ctl.core.scanner import *  # noqa: F403

sys.modules[__name__] = _scanner
