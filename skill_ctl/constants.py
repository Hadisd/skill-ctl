"""Constants and mappings for skill-ctl."""

from pathlib import Path

BASE_DIR = Path.home() / ".skill-ctl"
PRESETS_DIR = BASE_DIR / "presets"
CONFIG_FILE = BASE_DIR / "config.yaml"

AGENT_DIR_MAP = {
    "universal": ".agents/skills",
    "agents": ".agents/skills",
    "claude": ".claude/skills",
    "claude-code": ".claude/skills",
    "cursor": ".cursor/skills",
    "windsurf": ".codeium/windsurf/skills",
    "codex": ".codex/skills",
    "cline": ".agents/skills",
    "antigravity": ".agents/skills",
    "hermes": ".hermes/skills",
}

DEFAULT_TARGETS = [".agents/skills", ".claude/skills"]

# Sentinel that a bare -s/--skill expands to, meaning "prompt me".
SELECT_INTERACTIVELY = "?"

# What the preset prompt returns for "don't make me choose one": pick skills from
# every preset at once. Not a name any directory can have, so it cannot collide.
ALL_PRESETS = "\x00all-presets"

# Shown in the skill column of the picker for the row that means "all of it".
WHOLE_PRESET = "(whole preset)"
