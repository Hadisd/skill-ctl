"""Backward-compatibility proxy for skill_ctl.core.linker."""

import sys

from skill_ctl.core import linker as _linker
from skill_ctl.core.linker import *  # noqa: F403

sys.modules[__name__] = _linker
