"""Backward-compatibility proxy for skill_ctl.commands.search."""

import sys

from skill_ctl.commands import search as _search
from skill_ctl.commands.search import *  # noqa: F403

sys.modules[__name__] = _search
