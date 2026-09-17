"""Portable preset archive operations.

These functions work only with paths and archive data. Commands decide how to
present errors, choose names, and replace existing presets.
"""

import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ARCHIVE_FORMAT = 1


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
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        for path in sorted(source.rglob("*")):
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
                manifest = json.loads(archive.read("manifest.json"))
            except (KeyError, ValueError) as error:
                raise ValueError("missing or invalid manifest.json") from error
            if manifest.get("format") != ARCHIVE_FORMAT or not isinstance(manifest.get("preset"), str):
                raise ValueError("unsupported preset archive")
            for member in archive.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"unsafe archive entry: {member.filename}")
                if member.filename == "manifest.json":
                    continue
                if not member.filename.startswith("preset/"):
                    raise ValueError(f"unexpected archive entry: {member.filename}")
                target = destination / member_path.relative_to("preset")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not member.is_dir():
                    with archive.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"could not read archive: {error}") from error
    return manifest


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preset_changes(current: Path, incoming: Path) -> list[tuple[str, Path]]:
    """Return added, removed, and modified files between two preset directories."""
    current_files = {path.relative_to(current) for path in current.rglob("*") if path.is_file()}
    incoming_files = {path.relative_to(incoming) for path in incoming.rglob("*") if path.is_file()}
    changes = [("add", path) for path in sorted(incoming_files - current_files)]
    changes += [("remove", path) for path in sorted(current_files - incoming_files)]
    changes += [
        ("change", path) for path in sorted(current_files & incoming_files)
        if file_digest(current / path) != file_digest(incoming / path)
    ]
    return changes
