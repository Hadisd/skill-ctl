"""Filesystem operations used when applying and removing preset skills."""

import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
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


def rename_preset_directory(old_dir: Path, new_dir: Path, projects: dict[Path, list[str]]) -> None:
    """Move a preset and retarget its links, rolling back on replacement failure."""
    links = {path for path in old_dir.rglob("*") if path.is_symlink()}
    for project, targets in projects.items():
        for target in targets:
            directory = project / target
            if directory.is_symlink():
                links.add(directory)
            if directory.is_dir():
                links.update(path for path in directory.iterdir() if path.is_symlink())
    links = {path.parent.resolve() / path.name for path in links}
    bases = {
        old_dir.absolute(): new_dir.absolute(),
        old_dir.parent.resolve() / old_dir.name: new_dir.parent.resolve() / new_dir.name,
    }
    with ExitStack() as cleanup:
        replacements = []
        for link in sorted(links):
            original = os.readlink(link)
            target = Path(os.path.normpath(os.path.join(link.parent, original)))
            for old_base, new_base in bases.items():
                if not (target.is_relative_to(old_base) or any(b in target.parents for b in (old_base, old_base.resolve()))):
                    continue
                try:
                    rel_to_old = target.relative_to(old_base)
                except ValueError:
                    rel_to_old = target.resolve().relative_to(old_base.resolve())
                new_target = new_base / rel_to_old
                internal = any(b in link.parents for b in (old_base, old_base.resolve()))
                if internal:
                    try:
                        rel_link = link.relative_to(old_base)
                    except ValueError:
                        rel_link = Path(os.path.relpath(link, old_base))
                    moved_link = new_base / rel_link
                else:
                    moved_link = link
                replacement = str(new_target) if os.path.isabs(original) else os.path.relpath(new_target, moved_link.parent)
                temporary_parent = old_dir.parent if internal else link.parent
                temporary = Path(cleanup.enter_context(tempfile.TemporaryDirectory(
                    prefix=".skctl-rename-", dir=temporary_parent,
                )))
                updated = temporary / "updated"
                backup = temporary / "original"
                updated.symlink_to(replacement, target_is_directory=link.is_dir())
                backup.symlink_to(original, target_is_directory=link.is_dir())
                replacements.append((moved_link, updated, backup))
                break
        old_dir.rename(new_dir)
        replaced = []
        try:
            for link, updated, backup in replacements:
                if sys.platform == "win32" and (link.is_symlink() or link.exists()):
                    link.unlink()
                updated.replace(link)
                replaced.append((link, backup))
        except OSError:
            for link, backup in reversed(replaced):
                if sys.platform == "win32" and (link.is_symlink() or link.exists()):
                    link.unlink()
                backup.replace(link)
            new_dir.rename(old_dir)
            raise


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
    """Exact copied paths, never inferred from a skill/target cross product.

    Older records cannot prove which targets were skipped; require --force
    to refresh those copies instead of risking unrelated files.
    """
    entry = recorded.get(preset) or {}
    return set(entry.get("copies", []))


@dataclass
class LinkResult:
    applied: dict[str, Path]
    copied: bool
    kept: list[str]
    placed: set[str]
    copies: set[str]


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
    placed: set[str] = set()
    copies: set[str] = set()
    copied = copy
    for name, source in sorted(skills.items()):
        installed = False
        for relative_dir in target_dirs:
            norm_dest = Path(os.path.normpath(os.path.join(project, relative_dir, name)))
            if not norm_dest.is_relative_to(project.resolve()):
                kept.append(f"{relative_dir}/{name}")
                continue
            destination = project / relative_dir / name
            relative_path = f"{relative_dir}/{name}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                destination.unlink()
            elif destination.exists():
                if not (force or relative_path in owned):
                    kept.append(f"{relative_dir}/{name}")
                    continue
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if not copied:
                try:
                    destination.symlink_to(source.absolute(), target_is_directory=source.is_dir())
                    placed.add(relative_path)
                    installed = True
                    continue
                except OSError:
                    copied = True
            shutil.copytree(source, destination)
            placed.add(relative_path)
            copies.add(relative_path)
            installed = True
        if installed:
            applied[name] = source
    return LinkResult(applied, copied, kept, placed, copies)
