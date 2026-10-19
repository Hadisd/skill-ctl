"""Filesystem scanning for the skills inside a preset.

Kept apart from presets.py, which needs typer at import time for its command
signatures. Counting skills is pure filesystem work, so the hand-drawn root
help can read it without dragging a CLI framework in behind it.
"""

import os
from pathlib import Path


def _subdirs(path: Path) -> list[os.DirEntry]:
    """Directory entries under `path`, or [] when it is not a directory.

    scandir carries the file type on the entry itself, so is_dir() usually
    answers from the directory read instead of a stat() per skill.
    """
    try:
        with os.scandir(path) as entries:
            return [e for e in entries if not e.name.startswith(".") and e.is_dir()]
    except (FileNotFoundError, NotADirectoryError):
        return []


def get_preset_skills(preset_dir: Path) -> dict[str, Path]:
    """Finds all skills inside a preset directory.

    The agent folders win over a skill sitting directly in the preset, and
    .agents wins over .claude, so a skill linked into both is reported once.
    """
    skills: dict[str, Path] = {}

    # 1. Check .agents/skills
    for entry in _subdirs(preset_dir / ".agents" / "skills"):
        skills[entry.name] = Path(entry.path)

    # 2. Check .claude/skills
    for entry in _subdirs(preset_dir / ".claude" / "skills"):
        skills.setdefault(entry.name, Path(entry.path))

    # 3. Check direct subdirectories with SKILL.md
    with os.scandir(preset_dir) as entries:
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            if os.path.exists(os.path.join(entry.path, "SKILL.md")):
                skills.setdefault(entry.name, Path(entry.path))

    return skills
