"""
skill-ctl: AI Agent Skill & Preset Manager powered by npx skills (skills.sh)
"""

import importlib
import importlib.machinery
import importlib.util
import sys

__version__ = "0.1.19"

_MODULE_MAP = {
    # commands
    "backup": "skill_ctl.commands.backup",
    "catalog": "skill_ctl.commands.catalog",
    "completion": "skill_ctl.commands.completion",
    "config_commands": "skill_ctl.commands.config_commands",
    "diff": "skill_ctl.commands.diff",
    "doctor": "skill_ctl.commands.doctor",
    "preset_remote": "skill_ctl.commands.preset_remote",
    "presets": "skill_ctl.commands.presets",
    "search": "skill_ctl.commands.search",
    "self_update": "skill_ctl.commands.self_update",
    "skills": "skill_ctl.commands.skills",
    "status": "skill_ctl.commands.status",
    "sync": "skill_ctl.commands.sync",
    # core
    "archive": "skill_ctl.core.archive",
    "config": "skill_ctl.core.config",
    "constants": "skill_ctl.core.constants",
    "linker": "skill_ctl.core.linker",
    "registry": "skill_ctl.core.registry",
    "runner": "skill_ctl.core.runner",
    "scanner": "skill_ctl.core.scanner",
    "skillscan": "skill_ctl.core.skillscan",
    # ui
    "picker": "skill_ctl.ui.picker",
    "picker_filter": "skill_ctl.ui.picker_filter",
    "prompts": "skill_ctl.ui.prompts",
    "theme": "skill_ctl.ui.theme",
}


class _CompatibilityLoader:
    def __init__(self, target_name: str):
        self.target_name = target_name

    def create_module(self, spec):
        mod = importlib.import_module(self.target_name)
        sys.modules[spec.name] = mod
        return mod

    def exec_module(self, module):
        pass


class _CompatibilityFinder:
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname.startswith("skill_ctl."):
            sub = fullname[len("skill_ctl.") :]
            if sub in _MODULE_MAP:
                return importlib.machinery.ModuleSpec(
                    fullname,
                    _CompatibilityLoader(_MODULE_MAP[sub]),
                )
        return None


if not any(isinstance(finder, _CompatibilityFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _CompatibilityFinder())


def __getattr__(name: str):
    if name in _MODULE_MAP:
        mod = importlib.import_module(_MODULE_MAP[name])
        sys.modules[f"{__name__}.{name}"] = mod
        return mod
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
