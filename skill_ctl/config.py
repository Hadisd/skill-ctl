"""Configuration loading and management (~/.skill-ctl/config.yaml)."""

import functools
import warnings
from pathlib import Path
from typing import Optional

from skill_ctl.constants import BASE_DIR, CONFIG_FILE, DEFAULT_TARGETS, AGENT_DIR_MAP

DEFAULT_CONFIG_YAML = """# ~/.skill-ctl/config.yaml
# Configuration for skill-ctl (skctl)

# 1. Default destination when running 'skctl add <package>' without target flags
# Options:
#   - "prompt"   : (Default) Interactively ask [1-4] (preset, new preset, project, global)
#   - "preset"   : Automatically save into default preset (~/.skill-ctl/presets/<default_preset>/)
#   - "project"  : Automatically save directly into current project (.agents/skills/)
#   - "global"   : Automatically save globally (~/.agents/skills/)
default_destination: prompt

# 2. Default preset name when using presets
# Used when you run `skctl apply` or add to presets without specifying --preset
default_preset: default

# 3. Default agent targets for 'skctl apply'
# Project directories that receive symlinks/copies when applying a preset
apply_targets:
  - ".agents/skills"
  - ".claude/skills"

# 4. Installation method for 'skctl apply'
# Options: "symlink" (default) or "copy"
apply_mode: symlink

# 5. Default agents for skills.sh installation
# If empty []: skills.sh will prompt you interactively with its checklist.
# If populated (e.g. ["universal", "claude-code"]), uses these without prompting.
default_agents: []

# 6. Interactive prompt preferences
prompts:
  # Ask [1-4] destination menu if no -p, -g, or --preset flag is provided
  ask_destination: true
  # Let bare `skctl apply` pick the preset and its skills interactively
  ask_apply: true
  # How to ask which skills: "auto" uses fzf when it is installed and there is a
  # terminal to draw on, "fzf" always tries, "numbers" keeps the numbered list.
  picker: auto
  # Use fzf to choose a preset when more than this many are available.
  preset_picker_threshold: 10
  # After `presets create <name>` makes a brand-new empty preset, offer to search
  # skills.sh and add some to it right away. Only asked with a real terminal.
  ask_add_after_create: true

# 7. Custom agent folder mappings (extends built-in agents)
custom_agents: {}
"""

# Whole top-level sections added after the first release. A setting that belongs
# inside an existing block (prompts.picker, say) cannot be appended this way and
# simply takes its default until the user writes it in.
# Sections added after the first release. A config.yaml written by an older
# version is missing them, so `load_config` appends the ones it does not have
# rather than leaving settings the user cannot see in `skctl config show`.
LATER_SECTIONS = {
    "backup": """
# 8. GitHub backup of ~/.skill-ctl (see: skctl backup --help)
backup:
  # Push after every command that changes a preset, so you never forget to.
  # Needs `skctl backup init` first; a failed push only warns.
  auto_push: false
""",
    "npx": """
# 9. How skctl drives `npx skills`
npx:
  # `skctl add` hides the "Project / Global" scope question by patching the
  # scope check in the upstream cli.mjs as Node loads it. Turn this off to run
  # `npx skills` exactly as shipped and answer the question yourself.
  patch_scope_prompt: true
""",
    "global_skill_dirs": """
# 10. Global skill folders included in `skctl search` and preset creation
# Add custom shared-skill folders here, for example "~/skill-share".
global_skill_dirs:
  - "~/.agents/skills"
  - "~/.claude/skills"
  - "~/.codex/skills"
  - "~/.cursor/skills"
  - "~/.codeium/windsurf/skills"
""",
    "theme": """
# 11. Color theme for skctl's own output (see: skctl theme list)
# Options: "default", "dark", "light", "mono" (no color; NO_COLOR always forces
# this), "catppuccin-mocha", "tokyo-night", "gruvbox"
theme: default
""",
}

DEFAULT_CONFIG_YAML += "".join(LATER_SECTIONS.values())

def invalidate_config_cache() -> None:
    """Clear the cached config.yaml parse tree."""
    read_config_file.cache_clear()


def ensure_config() -> Path:
    """Create config.yaml, and add the sections a later version introduced.

    Everything that writes lives here, called once at startup, so that reading a
    setting through `load_config` never creates or rewrites anything on disk.
    """
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
        invalidate_config_cache()
        return CONFIG_FILE
    append_later_sections(read_config_file())
    return CONFIG_FILE


@functools.lru_cache(maxsize=1)
def read_config_file() -> dict:
    """The parsed config.yaml, or {} when it is absent or unreadable."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        import yaml
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        warnings.warn(f"Could not parse {CONFIG_FILE}: {e}", RuntimeWarning)
        return {}
    return data if isinstance(data, dict) else {}

def append_later_sections(data: dict) -> None:
    """Write the sections a config.yaml from an older version never got.

    Appending keeps the user's own settings and comments; only the blocks they
    are missing are added, commented as in the default file.
    """
    missing = [text for key, text in LATER_SECTIONS.items() if key not in data]
    if not missing:
        return
    try:
        with open(CONFIG_FILE, "a", encoding="utf-8") as f:
            f.write("".join(missing))
        invalidate_config_cache()
    except OSError as e:
        warnings.warn(f"Could not add new settings to {CONFIG_FILE}: {e}", RuntimeWarning)



def load_config() -> dict:
    """The effective settings. A pure read: it creates no file and no directory."""
    config = {
        "default_destination": "prompt",
        "default_preset": "default",
        "apply_targets": list(DEFAULT_TARGETS),
        "apply_mode": "symlink",
        "default_agents": [],
        "prompts": {
            "ask_destination": True,
            "ask_apply": True,
            "picker": "auto",
            "preset_picker_threshold": 10,
            "ask_add_after_create": True,
        },
        "custom_agents": {},
        "backup": {"auto_push": False},
        "npx": {"patch_scope_prompt": True},
        "theme": "default",
        "global_skill_dirs": [
            "~/.agents/skills",
            "~/.claude/skills",
            "~/.codex/skills",
            "~/.cursor/skills",
            "~/.codeium/windsurf/skills",
        ],
    }

    for k, v in read_config_file().items():
        if k in ("prompts", "backup", "npx") and isinstance(v, dict):
            config[k].update(v)
        elif k == "custom_agents" and isinstance(v, dict):
            config["custom_agents"].update(v)
        elif k in config:
            config[k] = v

    # A default_preset that is not a plain directory name would escape PRESETS_DIR
    # (`PRESETS_DIR / "/tmp/x"` is just "/tmp/x"), so refuse it rather than create
    # and later operate on a directory outside ~/.skill-ctl/presets.
    default_preset = config["default_preset"]
    if not isinstance(default_preset, str) or not default_preset.strip() \
            or default_preset in (".", "..") or "/" in default_preset or "\\" in default_preset:
        warnings.warn(f"Invalid default_preset {default_preset!r} in {CONFIG_FILE}; using 'default'.", RuntimeWarning)
        default_preset = "default"
    config["default_preset"] = default_preset

    return config


def global_skill_dirs(config: dict) -> list[Path]:
    """Configured global skill directories, expanded and deduplicated."""
    paths = []
    seen = set()
    configured = config.get("global_skill_dirs", [])
    if not isinstance(configured, list):
        warnings.warn("Invalid global_skill_dirs in config; expected a YAML list.", RuntimeWarning)
        return paths
    for value in configured:
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value).expanduser().resolve()
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def get_agent_dir_map(config: Optional[dict] = None) -> dict[str, str]:
    mapping = dict(AGENT_DIR_MAP)
    if config:
        mapping.update(config.get("custom_agents", {}))
    return mapping

def all_target_dirs(config: dict) -> list[str]:
    """Every relative directory a skill could have been applied to.

    `unapply` and `doctor` both have to look everywhere, not just at the
    configured apply_targets, since --agent could have put skills elsewhere.
    """
    return list(dict.fromkeys(
        list(get_agent_dir_map(config).values()) + list(config.get("apply_targets", DEFAULT_TARGETS))
    ))
