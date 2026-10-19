"""Backward-compatibility proxy for skill_ctl.core.registry."""

import sys

from skill_ctl.core import registry as _registry
from skill_ctl.core.registry import *  # noqa: F403

sys.modules[__name__] = _registry
