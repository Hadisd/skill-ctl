"""Configuration loading and management (~/.skill-ctl/config.yaml)."""

import functools
import os
import warnings
from pathlib import Path
from typing import Optional

from skill_ctl.core.constants import AGENT_DIR_MAP, BASE_DIR, CONFIG_FILE, DEFAULT_TARGETS

DEFAULT_MIN_RELEVANCE = 0.2
DEFAULT_SEARCH_LIMIT = 50

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
# If populated, uses these without prompting.
# Example:
#   default_agents:
#     - universal
#     - claude-code
#     - hermes
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
# Maps an agent name to its relative skills directory in a project.
# Example:
#   custom_agents:
#     hermes: ".hermes/skills"
#     my-agent: ".myagent/skills"
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
  # Hand your `gh auth token` to `npx skills` so GitHub lookups are not rate
  # limited. Off by default: `npx skills` is an unpinned npm package and the
  # token usually has broad scopes. A GITHUB_TOKEN/GH_TOKEN you already export
  # is always passed through.
  pass_github_token: false
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
  - "~/.hermes/skills"
""",
    "theme": """
# 11. Color theme for skctl's own output (see: skctl theme list)
# Options: "default", "dark", "light", "mono" (no color; NO_COLOR always forces
# this), "catppuccin-mocha", "tokyo-night", "gruvbox"
theme: default
""",
    "search_remotes": """
# 12. Remote skill search providers for `skctl search --remote`
search_remotes:
  - name: "skills.sh"
    url: "https://skills.sh"
    enabled: true
  - name: "skillsmp"
    url: "https://skillsmp.com"
    enabled: false
""",
    "search_min_relevance": """
# 13. Minimum relevance (0.0-1.0) a remote result needs to be listed.
# Results are ranked by how well the query matches the name (1.0 exact, 0.9
# prefix, ~0.8 word/substring), then source (0.5) and description (0.4), with
# popularity breaking ties. 0 shows everything the registries return; 0.5 keeps
# only results whose name or repo matches.
search_min_relevance: 0.2
""",
    "search_limit": """
# 14. Maximum number of results to fetch per remote registry (default: 50).
# Controls how many search results are retrieved from skills.sh and skillsmp.com.
search_limit: 50
""",
}

DEFAULT_CONFIG_YAML += "".join(LATER_SECTIONS.values())


def invalidate_config_cache() -> None:
    """Clear the cached config.yaml parse tree."""
    _read_raw_config.cache_clear()


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
    data = _read_raw_config()
    if data is not None:
        append_later_sections(data)
    return CONFIG_FILE


@functools.lru_cache(maxsize=1)
def _read_raw_config() -> Optional[dict]:
    """The parsed config.yaml, {} when absent/empty, or None when invalid/unreadable."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        import yaml

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        warnings.warn(f"Could not parse {CONFIG_FILE}: {e}", RuntimeWarning)
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        warnings.warn(f"Could not parse {CONFIG_FILE}: expected a YAML mapping.", RuntimeWarning)
        return None
    return data


def read_config_file() -> dict:
    """The parsed config.yaml, or {} when it is absent or unreadable."""
    data = _read_raw_config()
    return data if isinstance(data, dict) else {}


read_config_file.cache_clear = _read_raw_config.cache_clear


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


_DICT_SECTIONS = ("prompts", "backup", "npx", "custom_agents")
_STRING_LISTS = ("apply_targets", "default_agents", "global_skill_dirs")
_STRINGS = ("default_destination", "apply_mode", "theme")


def valid_preset_name(name: object) -> bool:
    """Whether *name* can be one preset directory: no separators, no dots-only, fits a filename."""
    if not isinstance(name, str) or not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    try:
        return len(os.fsencode(name)) <= 255
    except UnicodeError:
        return False


def _defaults() -> dict:
    """Fresh default settings; callers may mutate the result."""
    return {
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
        "npx": {"patch_scope_prompt": True, "pass_github_token": False},
        "theme": "default",
        "global_skill_dirs": [
            "~/.agents/skills",
            "~/.claude/skills",
            "~/.codex/skills",
            "~/.cursor/skills",
            "~/.codeium/windsurf/skills",
            "~/.hermes/skills",
        ],
        "search_remotes": [
            {"name": "skills.sh", "url": "https://skills.sh", "enabled": True},
            {"name": "skillsmp", "url": "https://skillsmp.com", "enabled": False},
        ],
        "search_min_relevance": DEFAULT_MIN_RELEVANCE,
        "search_limit": DEFAULT_SEARCH_LIMIT,
    }


def _warn_invalid(key: str, expected: str) -> None:
    warnings.warn(
        f"Invalid {key} in {CONFIG_FILE}; expected {expected}. Using the default.",
        RuntimeWarning,
        stacklevel=3,
    )


def _sanitize(config: dict) -> None:
    """Make every setting the type the rest of the code assumes, falling back to the default.

    A hand-edited config.yaml can hold `null` (a bare `npx:`), a scalar where a
    list belongs, or junk entries inside a list; none of that may crash a command.
    """
    defaults = _defaults()
    for key in _STRINGS:
        if not isinstance(config[key], str):
            _warn_invalid(key, "a string")
            config[key] = defaults[key]
    for key in _STRING_LISTS:
        value = config[key]
        if value is None and key != "default_agents":
            config[key] = list(defaults[key])
            continue
        if value is None:
            config[key] = []
            continue
        if not isinstance(value, list):
            _warn_invalid(key, "a list")
            config[key] = list(defaults[key])
            continue
        kept = [v for v in value if isinstance(v, str) and v.strip()]
        if value and not kept and key != "default_agents":
            _warn_invalid(key, "a list of non-empty strings")
            kept = list(defaults[key])
        config[key] = kept
    agents = config["custom_agents"]
    config["custom_agents"] = {
        k: v
        for k, v in agents.items()
        if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
    }
    if not isinstance(config["search_remotes"], list):
        _warn_invalid("search_remotes", "a list")
        config["search_remotes"] = defaults["search_remotes"]
    try:
        relevance = float(config["search_min_relevance"])
        if relevance != relevance:  # NaN
            raise ValueError
        config["search_min_relevance"] = relevance
    except (TypeError, ValueError):
        _warn_invalid("search_min_relevance", "a number")
        config["search_min_relevance"] = DEFAULT_MIN_RELEVANCE
    prompts = config["prompts"]
    if not isinstance(prompts.get("picker"), str):
        prompts["picker"] = defaults["prompts"]["picker"]
    try:
        prompts["preset_picker_threshold"] = int(prompts["preset_picker_threshold"])
    except (TypeError, ValueError):
        prompts["preset_picker_threshold"] = defaults["prompts"]["preset_picker_threshold"]


def load_config() -> dict:
    """The effective settings. A pure read: it creates no file and no directory."""
    config = _defaults()
    raw_file = read_config_file()
    for k, v in raw_file.items():
        if k in _DICT_SECTIONS:
            # A bare `npx:` is None: nothing configured, keep the defaults.
            if isinstance(v, dict):
                config[k].update(v)
            elif v is not None:
                _warn_invalid(k, "a mapping")
        elif k in config:
            config[k] = v
    _sanitize(config)

    try:
        raw_limit = raw_file.get("search_limit")
        if raw_limit is None:
            raw_limit = raw_file.get("search_query_limit")
        config["search_limit"] = (
            max(1, int(raw_limit)) if raw_limit is not None else DEFAULT_SEARCH_LIMIT
        )
    except (TypeError, ValueError):
        config["search_limit"] = DEFAULT_SEARCH_LIMIT

    # A default_preset that is not a plain directory name would escape PRESETS_DIR
    # (`PRESETS_DIR / "/tmp/x"` is just "/tmp/x"), so refuse it rather than create
    # and later operate on a directory outside ~/.skill-ctl/presets.
    default_preset = config["default_preset"]
    if not isinstance(default_preset, str) or not valid_preset_name(default_preset.strip()):
        warnings.warn(
            f"Invalid default_preset {default_preset!r} in {CONFIG_FILE}; using 'default'.",
            RuntimeWarning,
        )
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
    return list(
        dict.fromkeys(
            list(get_agent_dir_map(config).values())
            + list(config.get("apply_targets", DEFAULT_TARGETS))
        )
    )


def get_search_remotes(config: Optional[dict] = None) -> list[dict]:
    """Return enabled search remote providers, defaulting to skills.sh."""
    cfg = config or load_config()
    remotes = cfg.get("search_remotes")
    if isinstance(remotes, list) and remotes:
        return [r for r in remotes if isinstance(r, dict) and r.get("enabled", True)]
    return [
        {"name": "skills.sh", "url": "https://skills.sh", "enabled": True},
    ]


def project_skill_dirs(project: Path, config: dict) -> list[str]:
    """Every directory in this project that could hold skills."""
    found = list(all_target_dirs(config))
    if project == Path.home():
        return found
    for pattern in (".*/skills", ".*/*/skills"):
        for entry in sorted(project.glob(pattern)):
            rel = entry.relative_to(project).as_posix()
            if entry.is_dir() and rel not in found:
                found.append(rel)
    return found
