"""Backward-compatibility proxy for skill_ctl.commands.catalog."""

import sys

from skill_ctl.commands import catalog as _catalog
from skill_ctl.commands.catalog import *  # noqa: F403

sys.modules[__name__] = _catalog
