"""
skill-ctl: AI Agent Skill & Preset Manager powered by npx skills (skills.sh)
"""

import importlib
import importlib.machinery
import importlib.util
import sys

if sys.platform == "win32":
    # On Windows, CRT _isatty() returns True for character devices like NUL / DEVNULL.
    # Wrap sys.stdin.isatty to verify it is actually an interactive console handle via GetConsoleMode.
    try:
        import ctypes
        import msvcrt

        _orig_stdin_isatty = sys.stdin.isatty

        def _win_stdin_isatty():
            if not _orig_stdin_isatty():
                return False
            try:
                handle = msvcrt.get_osfhandle(sys.stdin.fileno())
                mode = ctypes.c_ulong()
                return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
            except Exception:
                return False

        sys.stdin.isatty = _win_stdin_isatty
    except Exception:
        pass

__version__ = "0.2.1"

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
        self._target_spec = None
        self._target_loader = None

    def _ensure_loader(self):
        if self._target_spec is None:
            self._target_spec = importlib.util.find_spec(self.target_name)
            self._target_loader = self._target_spec.loader if self._target_spec else None

    def create_module(self, spec):
        mod = importlib.import_module(self.target_name)
        sys.modules[spec.name] = mod
        return mod

    def exec_module(self, module):
        pass

    def get_code(self, fullname):
        self._ensure_loader()
        if self._target_loader and hasattr(self._target_loader, "get_code"):
            return self._target_loader.get_code(self.target_name)
        return None

    def get_source(self, fullname):
        self._ensure_loader()
        if self._target_loader and hasattr(self._target_loader, "get_source"):
            return self._target_loader.get_source(self.target_name)
        return None

    def is_package(self, fullname):
        self._ensure_loader()
        if self._target_loader and hasattr(self._target_loader, "is_package"):
            return self._target_loader.is_package(self.target_name)
        return False

    def get_filename(self, fullname):
        self._ensure_loader()
        if self._target_loader and hasattr(self._target_loader, "get_filename"):
            return self._target_loader.get_filename(self.target_name)
        return None


class _CompatibilityFinder:
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname.startswith("skill_ctl."):
            sub = fullname[len("skill_ctl.") :]
            if sub in _MODULE_MAP:
                target_name = _MODULE_MAP[sub]
                try:
                    target_spec = importlib.util.find_spec(target_name)
                    origin = target_spec.origin if target_spec else None
                except Exception:
                    origin = None
                return importlib.machinery.ModuleSpec(
                    fullname,
                    _CompatibilityLoader(target_name),
                    origin=origin,
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
