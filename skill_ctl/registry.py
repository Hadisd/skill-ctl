"""Which presets are applied to which projects.

`apply` records what it linked twice over. The project's own skills-applied.json
is the truth, small enough to commit, so a clone of the project knows what it
uses. ~/.skill-ctl/applied.json indexes the same thing by path, which is what lets
`doctor --all` and `presets delete` see across projects without hunting for them.

It sits beside skills-lock.json rather than in it: that file is upstream's record
of where a skill came from, rewritten wholesale on every `npx skills` write, and
keyed by skill name, so it has no room for which preset, which agent directories,
or symlink versus copy.
"""

import json
import warnings
from pathlib import Path
from typing import Iterable

from skill_ctl.constants import BASE_DIR

APPLIED_FILE = BASE_DIR / "applied.json"
PROJECT_FILE = "skills-applied.json"

# What the file was called before it had a name that says what it holds. Read for
# as long as projects carry one; the next write replaces it.
LEGACY_PROJECT_FILES = (".skctl",)


def project_file_path(project: Path) -> Path:
    """Where this project's record is, preferring the current name."""
    path = project / PROJECT_FILE
    if path.exists():
        return path
    for legacy in LEGACY_PROJECT_FILES:
        if (project / legacy).exists():
            return project / legacy
    return path


def read_project_file(project: Path) -> dict:
    """The presets recorded in the project itself, if it has a record."""
    try:
        data = json.loads(project_file_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    presets = data.get("presets") if isinstance(data, dict) else None
    return presets if isinstance(presets, dict) else {}


def write_project_file(project: Path, presets: dict) -> None:
    """Rewrite the project's record, deleting it once nothing is applied.

    Always under the current name, so a project carrying the old one is migrated
    by the first command that changes anything.
    """
    path = project / PROJECT_FILE
    try:
        if presets:
            path.write_text(
                json.dumps({"version": 1, "presets": presets}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif path.exists():
            path.unlink()
        for legacy in LEGACY_PROJECT_FILES:
            if (project / legacy).exists():
                (project / legacy).unlink()
    except OSError as e:
        warnings.warn(f"Could not update {path}: {e}", RuntimeWarning)


def load_applied() -> dict:
    """Every recorded project, keyed by absolute path. Unreadable file means none."""
    try:
        data = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}


def save_applied(projects: dict) -> None:
    try:
        if projects:
            APPLIED_FILE.parent.mkdir(parents=True, exist_ok=True)
            APPLIED_FILE.write_text(
                json.dumps({"version": 1, "projects": projects}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif APPLIED_FILE.exists():
            APPLIED_FILE.unlink()
    except OSError as e:
        warnings.warn(f"Could not update {APPLIED_FILE.name}: {e}", RuntimeWarning)


def record_apply(project: Path, preset: str, skills: Iterable[str], targets: Iterable[str], mode: str) -> None:
    """Add to what this project has applied from `preset`.

    The record is the union of every apply, because the links are: applying with
    `-s a` and then `-s b` leaves both on disk, and an entry naming only `b` would
    hide `a` from `unapply` and `--resync` for good. Only one mode fits an entry,
    so the newest wins; a project that mixes symlinks and copies from one preset
    needs `unapply --force` to clear the copies.
    """
    in_project = read_project_file(project)
    previous = in_project.get(preset) or {}
    entry = {
        "skills": sorted(set(previous.get("skills", [])) | set(skills)),
        "targets": list(dict.fromkeys(list(previous.get("targets", [])) + list(targets))),
        "mode": mode,
    }

    in_project[preset] = entry
    write_project_file(project, in_project)

    projects = load_applied()
    projects[str(project)] = in_project
    save_applied(projects)


def forget_apply(project: Path, preset: str) -> None:
    in_project = read_project_file(project)
    in_project.pop(preset, None)
    write_project_file(project, in_project)

    projects = load_applied()
    if str(project) in projects:
        if in_project:
            projects[str(project)] = in_project
        else:
            del projects[str(project)]
        save_applied(projects)


def keep_only(project: Path, preset: str, skills: Iterable[str]) -> None:
    """Narrow a recorded entry to the skills still applied, forgetting it if none are.

    A partial `unapply` leaves things behind - a copy, or a skill installed by
    hand - and the record is what `unapply --force` uses to find them again, so it
    must survive that run while naming only what is actually still there.
    """
    in_project = read_project_file(project)
    entry = in_project.get(preset)
    if not entry:
        return
    remaining = sorted(set(entry.get("skills", [])) & set(skills))
    if remaining:
        entry["skills"] = remaining
        in_project[preset] = entry
    else:
        del in_project[preset]
    write_project_file(project, in_project)

    projects = load_applied()
    if in_project:
        projects[str(project)] = in_project
    else:
        projects.pop(str(project), None)
    save_applied(projects)


def presets_for(project: Path) -> dict:
    """What this project has applied, according to the project itself.

    A project cloned from elsewhere carries the record but is missing from this
    machine's index, so reading one re-indexes it and `doctor --all` finds it.
    """
    in_project = read_project_file(project)
    if in_project:
        projects = load_applied()
        if projects.get(str(project)) != in_project:
            projects[str(project)] = in_project
            save_applied(projects)
        return in_project
    return load_applied().get(str(project), {})


def projects_using(preset: str) -> list[str]:
    return sorted(path for path, presets in load_applied().items() if preset in presets)
