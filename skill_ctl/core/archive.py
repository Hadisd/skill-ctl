"""Portable preset archive operations.

These functions work only with paths and archive data. Commands decide how to
present errors, choose names, and replace existing presets.
"""

import hashlib
import json
import os
import shutil
import stat
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

ARCHIVE_FORMAT = 1
MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


def export_preset(source: Path, name: str, destination: Path, skills: list[str]) -> None:
    """Write one preset and its metadata to a new zip archive."""
    lock = source / "skills-lock.json"
    try:
        source_metadata = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else None
    except ValueError:
        source_metadata = None
    manifest = {
        "format": ARCHIVE_FORMAT,
        "preset": name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "skills": sorted(skills),
        "source_metadata": source_metadata,
    }
    source_root = source.resolve()
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                try:
                    resolved = path.resolve()
                except (OSError, RuntimeError, ValueError):
                    continue
                if not resolved.is_relative_to(source_root):
                    continue
                if path.is_dir():
                    continue
            archive_name = Path("preset") / path.relative_to(source)
            if path.is_dir():
                archive.writestr(archive_name.as_posix() + "/", "")
            elif path.is_file():
                archive.write(path, archive_name.as_posix())


def safe_extract_preset(archive_path: Path, destination: Path) -> dict:
    """Extract a preset archive, rejecting traversal and unexpected members."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            try:
                manifest_info = archive.getinfo("manifest.json")
            except KeyError as error:
                raise ValueError("missing or invalid manifest.json") from error
            if manifest_info.file_size > MAX_ARCHIVE_BYTES:
                raise ValueError(f"archive expands beyond {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB")
            with archive.open(manifest_info) as mf:
                raw_manifest = mf.read(MAX_ARCHIVE_BYTES + 1)
            if len(raw_manifest) > MAX_ARCHIVE_BYTES:
                raise ValueError(f"archive expands beyond {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB")
            try:
                manifest = json.loads(raw_manifest)
            except ValueError as error:
                raise ValueError("missing or invalid manifest.json") from error
            if manifest.get("format") != ARCHIVE_FORMAT or not isinstance(
                manifest.get("preset"), str
            ):
                raise ValueError("unsupported preset archive")
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError(f"too many entries (limit {MAX_ARCHIVE_FILES})")
            if sum(member.file_size for member in members) > MAX_ARCHIVE_BYTES:
                raise ValueError(f"archive expands beyond {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB")
            destination.mkdir(parents=True, exist_ok=True)
            root = destination.resolve()
            extracted_bytes = 0
            for member in members:
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"unsafe archive entry: {member.filename}")
                if member.filename == "manifest.json":
                    continue
                if not member.filename.startswith("preset/"):
                    raise ValueError(f"unexpected archive entry: {member.filename}")
                target = destination / member_path.relative_to("preset")
                # Catches what the checks above cannot, e.g. drive letters on Windows.
                if not target.resolve().is_relative_to(root):
                    raise ValueError(f"unsafe archive entry: {member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as output:
                        while True:
                            chunk = source.read(65536)
                            if not chunk:
                                break
                            extracted_bytes += len(chunk)
                            if extracted_bytes > MAX_ARCHIVE_BYTES:
                                raise ValueError(
                                    f"archive expands beyond {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB"
                                )
                            output.write(chunk)
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"could not read archive: {error}") from error
    return manifest


def _remove_tree(path: Path) -> None:
    """Best-effort removal of a staged backup directory or symlink across platforms."""
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
            return

        def _chmod_and_retry(func, failed_path, _exc):
            os.chmod(failed_path, stat.S_IWRITE | stat.S_IREAD)
            func(failed_path)

        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_chmod_and_retry)
        else:
            shutil.rmtree(path, onerror=_chmod_and_retry)
    except OSError:
        pass


def replace_directory_atomically(staged: Path, target: Path) -> None:
    """Replace *target* with *staged* (same filesystem), restoring on error."""
    backup: Optional[Path] = None
    if target.exists() or target.is_symlink():
        backup = staged.parent / ".skctl-backup"
        counter = 0
        while backup.exists() or backup.is_symlink():
            counter += 1
            backup = staged.parent / f".skctl-backup-{counter}"
        target.rename(backup)
    try:
        staged.rename(target)
    except OSError:
        if (
            backup is not None
            and (backup.exists() or backup.is_symlink())
            and not (target.exists() or target.is_symlink())
        ):
            backup.rename(target)
        raise
    if backup is not None and (backup.exists() or backup.is_symlink()):
        _remove_tree(backup)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preset_files(root: Path) -> set[Path]:
    """Relative paths of the regular files under *root*, never following symlinks."""
    found = set()
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if not path.is_symlink():
                found.add(path.relative_to(root))
    return found


def preset_symlinks(root: Path) -> list[Path]:
    """Relative paths of every symlink (file or directory) under *root*."""
    found = []
    for directory, dirs, names in os.walk(root):
        for name in (*dirs, *names):
            path = Path(directory) / name
            if path.is_symlink():
                found.append(path.relative_to(root))
    return sorted(found)


def copy_preset(source: Path, target: Path) -> list[Path]:
    """Copy an untrusted preset, dropping symlinks instead of following them.

    A symlink in a downloaded preset could point at a local secret; copying its
    target would turn that secret into a plain file in the preset (and in the
    next backup push). Returns the skipped symlinks, relative to *source*.
    """
    skipped = preset_symlinks(source)
    shutil.copytree(
        source,
        target,
        symlinks=True,
        ignore=lambda directory, names: [n for n in names if (Path(directory) / n).is_symlink()],
    )
    return skipped


def link_escapes(path: Path, root: Path) -> bool:
    """Whether *path* is a symlink that resolves outside *root* (a resolved path)."""
    if not path.is_symlink():
        return False
    try:
        return not path.resolve().is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return True


def copy_skill_tree(
    source: Path,
    destination: Path,
    ignore: Optional[Callable[[str, list[str]], Iterable[str]]] = None,
) -> list[Path]:
    """Copy a skill folder, dropping symlinks that resolve outside of it.

    Following such a link would copy an unrelated local file (an SSH key, say)
    into the destination; for a backup that destination is pushed to a remote.
    Links that stay inside the folder are kept as links. Returns the dropped
    links, relative to *source*.
    """
    root = source.resolve()
    dropped: list[Path] = []

    def skip(directory: str, names: list[str]) -> set[str]:
        skipped = set(ignore(directory, names)) if ignore else set()
        for name in names:
            path = Path(directory) / name
            if name not in skipped and link_escapes(path, root):
                skipped.add(name)
                dropped.append(path.relative_to(source))
        return skipped

    shutil.copytree(source, destination, symlinks=True, ignore=skip)
    return sorted(dropped)


def preset_changes(current: Path, incoming: Path) -> list[tuple[str, Path]]:
    """Return added, removed, and modified files between two preset directories."""
    current_files = preset_files(current)
    incoming_files = preset_files(incoming)
    changes = [("add", path) for path in sorted(incoming_files - current_files)]
    changes += [("remove", path) for path in sorted(current_files - incoming_files)]
    changes += [
        ("change", path)
        for path in sorted(current_files & incoming_files)
        if file_digest(current / path) != file_digest(incoming / path)
    ]
    return changes
