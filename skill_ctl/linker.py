"""Filesystem operations used when applying and removing preset skills."""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


def links_into_preset(path: Path, preset_dir: Path) -> bool:
    """Whether a symlink points into a preset without resolving its source."""
    if not path.is_symlink():
        return False
    target = Path(os.path.normpath(os.path.join(path.parent, os.readlink(path))))
    bases = {preset_dir, preset_dir.resolve()}
    return any(base in candidate.parents for candidate in (target, path.resolve()) for base in bases)


def prune_lockfile(project: Path, removed_names: set[str]) -> int:
    """Remove deleted skills from a project's skills.sh lockfile."""
    lock = project / "skills-lock.json"
    if not removed_names or not lock.is_file():
        return 0
    data = json.loads(lock.read_text(encoding="utf-8"))
    entries = data.get("skills")
    if not isinstance(entries, dict):
        return 0
    stale = [name for name in entries if name in removed_names]
    if not stale:
        return 0
    for name in stale:
        del entries[name]
    if not entries and set(data) <= {"version", "skills"}:
        lock.unlink()
    else:
        lock.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return len(stale)


def prune_empty_dirs(path: Path, stop: Path) -> Optional[Path]:
    """Delete empty parents below *stop*, returning the outermost deleted path."""
    removed = None
    while path != stop and stop in path.parents and path.is_dir() and not any(path.iterdir()):
        path.rmdir()
        removed = path
        path = path.parent
    return removed


def skill_kinds(project: Path, rel_dirs: list[str], names: Iterable[str]) -> dict[tuple[str, str], str]:
    """Classify existing skill paths as links or real files."""
    kinds = {}
    for rel_dir in rel_dirs:
        for name in names:
            path = project / rel_dir / name
            if path.is_symlink():
                kinds[(rel_dir, name)] = "link"
            elif path.exists():
                kinds[(rel_dir, name)] = "real"
    return kinds


def owned_copies(recorded: dict, preset: str) -> set[str]:
    entry = recorded.get(preset) or {}
    return set(entry.get("skills", [])) if entry.get("mode") == "copy" else set()


@dataclass
class LinkResult:
    applied: dict[str, Path]
    copied: bool
    kept: list[str]


def apply_skills(
    project: Path,
    skills: dict[str, Path],
    target_dirs: list[str],
    copy: bool,
    force: bool = False,
    owned: Optional[set[str]] = None,
) -> LinkResult:
    """Link or copy skills into target directories without presentation or registry work."""
    owned = owned or set()
    applied: dict[str, Path] = {}
    kept: list[str] = []
    copied = copy
    for name, source in sorted(skills.items()):
        installed = False
        for relative_dir in target_dirs:
            destination = project / relative_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                destination.unlink()
            elif destination.exists():
                if not (force or name in owned):
                    kept.append(f"{relative_dir}/{name}")
                    continue
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if not copied:
                try:
                    destination.symlink_to(source.absolute())
                    installed = True
                    continue
                except OSError:
                    copied = True
            shutil.copytree(source, destination)
            installed = True
        if installed:
            applied[name] = source
    return LinkResult(applied, copied, kept)
