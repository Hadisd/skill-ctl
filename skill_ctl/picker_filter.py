"""Backward-compatibility proxy for skill_ctl.ui.picker_filter."""

from skill_ctl.ui.picker_filter import *  # noqa: F403
from skill_ctl.ui.picker_filter import prioritize_names  # noqa: F401

if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lines = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print("\n".join(prioritize_names(lines, sys.argv[2])))

