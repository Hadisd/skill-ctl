"""Backward-compatibility proxy for skill_ctl.ui.picker."""

import sys

from skill_ctl.ui import picker as _picker
from skill_ctl.ui.picker import (  # noqa: F401
    DEFAULT_HEADER,
    format_global_location,
    parse_skill_meta,
    pick,
    pick_with_fzf,
    preview_command,
    preview_markdown_command,
    rows_for,
    skill_meta,
    truncate,
    wants_picker,
)

sys.modules[__name__] = _picker

