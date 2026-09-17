"""Preset storage and application logic (apply, unapply, presets)."""

import json
import os
import re
import shlex
import sys
import shutil
import hashlib
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Iterable, Literal, Optional

import typer

# rich.table and rich.progress are imported where they are used: only a few
# commands render a table or a progress bar, and every command imports this
# module, so at module level they cost each invocation ~13ms for nothing.
from rich.prompt import Confirm, Prompt

# Beyond this many skills, per-skill lines scroll the summary away, so the
# commands switch to a progress bar and report totals instead.
LIST_LIMIT = 25

# How many names one row of the presets table shows before it says "+N more":
# a preset of a few hundred skills would otherwise be the whole screen.
TABLE_LIMIT = 8

from skill_ctl.constants import ALL_PRESETS, PRESETS_DIR, CONFIG_FILE, DEFAULT_TARGETS, SELECT_INTERACTIVELY
from skill_ctl.config import load_config, get_agent_dir_map, all_target_dirs, global_skill_dirs
from skill_ctl.ui import console, print_header, print_success, print_warn, print_error
from skill_ctl.prompts import prompt_new_preset_name, prompt_preset, prompt_skills
from skill_ctl.archive import export_preset as write_archive, safe_extract_preset, preset_changes
from skill_ctl.linker import apply_skills, links_into_preset, prune_lockfile, prune_empty_dirs, skill_kinds, owned_copies
from skill_ctl.picker import DEFAULT_HEADER, pick, rows_for, wants_picker
from skill_ctl.runner import run_npx_skills
# Re-exported: it lives in its own module so callers that only count skills
# need not import this one, but it stays reachable as presets.get_preset_skills.
from skill_ctl.skillscan import get_preset_skills
from skill_ctl.theme import fzf_color_arg, picker_ansi
from skill_ctl.registry import (
    PROJECT_FILE, load_applied, save_applied, read_project_file, write_project_file,
    record_apply, forget_apply, keep_only, presets_for, projects_using,
)
from skill_ctl.backup import maybe_auto_push

def preset_path(preset_name: str) -> Path:
    """Resolve a preset name to its directory, rejecting anything that escapes
    PRESETS_DIR ('..', a nested path, or an absolute path - `PRESETS_DIR / "/x"`
    is just "/x", and `presets delete` would then rmtree it)."""
    name = str(preset_name)
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        print_error(f"Invalid preset name '{name}': must be a single directory name.")
        sys.exit(1)
    return PRESETS_DIR / name

def ensure_preset_dir(preset_name: str = "default") -> Path:
    p = preset_path(preset_name)
    p.mkdir(parents=True, exist_ok=True)
    return p


ARCHIVE_FORMAT = 1
HISTORY_FILE = ".skctl.json"
LEGACY_HISTORY_FILE = ".skctl-history.json"


def _write_history(preset_dir: Path, action: str, sources: list[str], skills: Iterable[str]) -> None:
    """Record how a newly cloned or combined preset was created."""
    event = {
        "action": action,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "skills": sorted(skills),
    }
    (preset_dir / HISTORY_FILE).write_text(json.dumps({"events": [event]}, indent=2) + "\n", encoding="utf-8")


def _history_events(preset_dir: Path) -> list[dict]:
    try:
        history_file = preset_dir / HISTORY_FILE
        if not history_file.exists():
            history_file = preset_dir / LEGACY_HISTORY_FILE
        events = json.loads(history_file.read_text(encoding="utf-8")).get("events", [])
        return events if isinstance(events, list) else []
    except (OSError, ValueError):
        return []


def preset_history(name: str) -> None:
    """Print the recorded clone or combine origin for one preset."""
    preset_dir = preset_path(name)
    if not preset_dir.is_dir():
        print_error(f"Preset '{name}' does not exist.")
        sys.exit(1)
    events = _history_events(preset_dir)
    if not events:
        console.print(f"[dim]No clone or combine history recorded for '{name}'.[/dim]")
        return
    print_header(f"History: {name}")
    for event in events:
        sources = ", ".join(event.get("sources", []))
        console.print(f"  {event.get('at', '')}  [header]{event.get('action', '')}[/header]  {sources}")


def preset_preview(preset_dir: Path, projects: list[str]) -> str:
    """Build the fzf preview for a preset, including its recorded origin."""
    skills = sorted(get_preset_skills(preset_dir))
    text = (
        f"Preset: {preset_dir.name}\nLocation: {preset_dir}\nApplied projects ({len(projects)}):\n"
        + "\n".join(f"  {project}" for project in projects)
        + f"\n\nSkills ({len(skills)}):\n"
        + "\n".join(f"  {skill}" for skill in skills)
    )
    events = _history_events(preset_dir)
    if events:
        text += "\n\nHistory:\n" + "\n".join(
            f"  {event.get('at', '')}  {event.get('action', '')}  {', '.join(event.get('sources', []))}"
            for event in events
        )
    return text


def preset_export(
    name: str,
    output: Optional[str] = None,
) -> None:
    """Write one preset, its files, and its source lock data to a zip archive."""
    source = preset_path(name)
    if not source.is_dir():
        print_error(f"Preset '{name}' does not exist.")
        sys.exit(1)
    destination = Path(output).expanduser() if output else Path.cwd() / f"{name}.skctl-preset.zip"
    if destination.exists():
        print_error(f"Archive already exists: {destination}. Choose another --output path.")
        sys.exit(1)
    try:
        write_archive(source, name, destination, list(get_preset_skills(source)))
    except (OSError, zipfile.BadZipFile) as error:
        print_error(f"Could not export '{name}': {error}")
        sys.exit(1)
    print_success(f"Exported preset '{name}' to {destination}")


def _preset_destination(name: str, replace: bool) -> Path:
    """Validate a new preset destination without touching it."""
    destination = preset_path(name)
    if destination.exists() and not replace:
        print_error(f"Preset '{name}' already exists. Use --replace to overwrite it.")
        sys.exit(1)
    return destination


def clone_preset(source_name: str, destination_name: str, replace: bool = False, dry_run: bool = False) -> None:
    """Copy one preset exactly into a new independent preset."""
    source = preset_path(source_name)
    if not source.is_dir():
        print_error(f"Preset '{source_name}' does not exist.")
        sys.exit(1)
    if source_name == destination_name:
        print_error("A preset cannot be cloned onto itself.")
        sys.exit(1)
    destination = _preset_destination(destination_name, replace)
    verb = "replace" if destination.exists() else "create"
    if dry_run:
        console.print(f"Would {verb} preset '{destination_name}' by cloning '{source_name}'.")
        return
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    _write_history(destination, "clone", [source_name], get_preset_skills(destination))
    print_success(f"Cloned preset '{source_name}' to '{destination_name}'")
    maybe_auto_push(f"clone preset {source_name} to {destination_name}")
    offer_add_after_create(destination_name, destination, load_config())


def combine_presets(destination_name: str, source_names: list[str], replace: bool = False, dry_run: bool = False) -> None:
    """Copy the effective skills from several presets into one flat preset."""
    if len(source_names) < 2:
        print_error("Choose at least two presets. Usage: skctl presets combine <new-name> <preset> <preset> [...]")
        sys.exit(1)
    if len(set(source_names)) != len(source_names):
        print_error("Choose each source preset only once.")
        sys.exit(1)
    if destination_name in source_names:
        print_error("The new preset cannot also be a source preset.")
        sys.exit(1)
    sources = [(name, preset_path(name)) for name in source_names]
    missing = [name for name, path in sources if not path.is_dir()]
    if missing:
        print_error(f"Preset not found: {', '.join(missing)}")
        sys.exit(1)
    destination = _preset_destination(destination_name, replace)

    skills: dict[str, tuple[str, Path]] = {}
    duplicates: list[tuple[str, str, str]] = []
    for source_name, source in sources:
        for skill_name, skill_path in get_preset_skills(source).items():
            if skill_name in skills:
                kept_name, _ = skills[skill_name]
                if sys.stdin.isatty() and not dry_run:
                    console.print(f"[warn]Conflict:[/warn] {skill_name}")
                    console.print(f"  [choice]1)[/choice] Keep {kept_name}")
                    console.print(f"  [choice]2)[/choice] Use {source_name}")
                    choice = Prompt.ask("Choose an option", choices=["1", "2"], default="1", console=console)
                    if choice == "2":
                        skills[skill_name] = (source_name, skill_path)
                        duplicates.append((skill_name, source_name, kept_name))
                        continue
                duplicates.append((skill_name, skills[skill_name][0], source_name))
                continue
            skills[skill_name] = (source_name, skill_path)

    verb = "replace" if destination.exists() else "create"
    if dry_run:
        console.print(f"Would {verb} preset '{destination_name}' from: {', '.join(source_names)}")
        for skill_name, (source_name, _) in sorted(skills.items()):
            console.print(f"  copy {skill_name} from {source_name}")
        for skill_name, kept, skipped in duplicates:
            console.print(f"  keep {skill_name} from {kept}; skip duplicate from {skipped}")
        return

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for skill_name, (_, skill_path) in skills.items():
        shutil.copytree(skill_path, destination / skill_name)
    _write_history(destination, "combine", source_names, skills)
    print_success(f"Combined {len(skills)} skills into preset '{destination_name}'")
    if duplicates:
        print_warn(f"Kept the first selected version of {len(duplicates)} duplicate skill(s).")
    maybe_auto_push(f"combine presets into {destination_name}")
    offer_add_after_create(destination_name, destination, load_config())


def _safe_extract_preset(archive_path: Path, destination: Path) -> dict:
    """Extract only the archive's preset directory, rejecting path traversal."""
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


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _preset_changes(current: Path, incoming: Path) -> list[tuple[str, Path]]:
    current_files = {path.relative_to(current) for path in current.rglob("*") if path.is_file()}
    incoming_files = {path.relative_to(incoming) for path in incoming.rglob("*") if path.is_file()}
    changes = [("add", path) for path in sorted(incoming_files - current_files)]
    changes += [("remove", path) for path in sorted(current_files - incoming_files)]
    changes += [("change", path) for path in sorted(current_files & incoming_files)
                if _file_digest(current / path) != _file_digest(incoming / path)]
    return changes


def preset_import(
    archive: str,
    rename: Optional[str] = None,
    replace: bool = False,
    dry_run: bool = False,
) -> None:
    """Preview or import a portable preset archive without silent replacement."""
    archive_path = Path(archive).expanduser()
    if not archive_path.is_file():
        print_error(f"Preset archive not found: {archive_path}")
        sys.exit(1)
    with tempfile.TemporaryDirectory(prefix="skctl-import-") as temporary:
        extracted = Path(temporary) / "preset"
        try:
            manifest = safe_extract_preset(archive_path, extracted)
        except ValueError as error:
            print_error(f"Cannot import {archive_path}: {error}")
            sys.exit(1)
        target_name = rename or manifest["preset"]
        target = preset_path(target_name)
        changes = preset_changes(target, extracted) if target.exists() else [
            ("add", path.relative_to(extracted)) for path in extracted.rglob("*") if path.is_file()
        ]
        print_header(f"Import preview: '{manifest['preset']}' -> '{target_name}'")
        for action, path in changes:
            console.print(f"  {action:6} {path}")
        if not changes:
            console.print("  [dim]no file changes[/dim]")
        if dry_run:
            return
        if target.exists() and not replace:
            print_error(f"Preset '{target_name}' already exists. Use --replace or --rename <name>.")
            sys.exit(1)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(extracted, target)
    print_success(f"Imported preset '{target_name}' from {archive_path}")
    maybe_auto_push(f"import preset {target_name}")

def copy_skills_from_path(source_path: str, destination: Path) -> int:
    """Copy direct skills from a skill root, agent folder, or project directory."""
    source = Path(source_path).expanduser().resolve()
    if not source.is_dir():
        print_error(f"Skill source is not a directory: {source}")
        sys.exit(1)
    roots = [source / "skills", source / ".agents" / "skills", source / ".claude" / "skills", source]
    skills: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and not entry.name.startswith(".") and (entry / "SKILL.md").is_file():
                skills.setdefault(entry.name, entry)
    if not skills:
        print_error(f"No direct skill folders found in {source}")
        sys.exit(1)
    copied = 0
    for name, skill in sorted(skills.items()):
        target = destination / name
        if target.exists():
            print_warn(f"Skipping {name}; it is already in '{destination.name}'.")
            continue
        shutil.copytree(skill, target)
        copied += 1
    print_success(f"Copied {copied} skill{'s' if copied != 1 else ''} from {source} to '{destination.name}'")
    return copied


def get_global_skills(config: dict) -> list:
    """Skills in configured global folders, keeping their folder as the source."""
    rows = []
    for directory in global_skill_dirs(config):
        if not directory.is_dir():
            continue
        skills = {
            entry.name: entry for entry in directory.iterdir()
            if entry.is_dir() and not entry.name.startswith(".") and (entry / "SKILL.md").is_file()
        }
        label = "~/" + directory.relative_to(Path.home()).as_posix() \
            if directory.is_relative_to(Path.home()) else str(directory)
        rows.extend(rows_for(label, skills, scope="global"))
    return rows


def preview_apply(
    target_project: Path,
    target_preset: str,
    skills: dict[str, Path],
    dest_rel_dirs: list[str],
    use_copy: bool,
    force: bool,
    owned: set[str],
) -> None:
    """Print apply's filesystem decisions without changing the project."""
    verb = "copy" if use_copy else "link"
    print_header(f"Dry run: applying preset '{target_preset}' to {target_project}")
    for skill_name in sorted(skills):
        for rel_dir in dest_rel_dirs:
            destination = target_project / rel_dir / skill_name
            label = f"{rel_dir}/{skill_name}"
            if destination.is_symlink():
                console.print(f"  Would replace link {label} with {verb}")
            elif destination.exists():
                if not (force or skill_name in owned):
                    console.print(f"  Would keep existing {label} (use --force to replace)")
                else:
                    console.print(f"  Would replace {label} with {verb}")
            else:
                console.print(f"  Would {verb} {label}")


def read_global_lock() -> dict:
    """Upstream metadata for globally installed skills, keyed by skill name."""
    try:
        data = json.loads((Path.home() / ".agents" / ".skill-lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    skills = data.get("skills") if isinstance(data, dict) else None
    return skills if isinstance(skills, dict) else {}

def _legacy_links_into_preset(path: Path, preset_dir: Path) -> bool:
    """True if path is a symlink pointing inside the given preset directory.

    The link target is compared without following further symlinks first: a preset
    skill may itself be a symlink into npm's store (that is what `npx skills add`
    creates), and fully resolving would then point outside the preset.
    """
    if not path.is_symlink():
        return False
    target = Path(os.path.normpath(os.path.join(path.parent, os.readlink(path))))
    bases = {preset_dir, preset_dir.resolve()}
    return any(b in c.parents for c in (target, path.resolve()) for b in bases)

def _legacy_prune_lockfile(project: Path, removed_names: set) -> int:
    """Drop the skills we just deleted from the project's skills-lock.json, which
    `apply --npx` writes. Stale entries would otherwise come back on an update."""
    lock = project / "skills-lock.json"
    if not removed_names or not lock.is_file():
        return 0
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
        entries = data.get("skills")
        if not isinstance(entries, dict):
            return 0
        stale = [name for name in entries if name in removed_names]
        if not stale:
            return 0
        for name in stale:
            del entries[name]
        # Drop the file only once we emptied it ourselves, and only when it holds
        # nothing else worth keeping.
        if not entries and set(data) <= {"version", "skills"}:
            lock.unlink()
        else:
            lock.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return len(stale)
    except (OSError, ValueError) as e:
        print_warn(f"Could not update {lock.name}: {e}")
        return 0


def _legacy_prune_empty_dirs(path: Path, stop: Path) -> Optional[Path]:
    """Delete path and any empty parents below stop. Returns the outermost removed."""
    removed = None
    while path != stop and stop in path.parents and path.is_dir() and not any(path.iterdir()):
        path.rmdir()
        removed = path
        path = path.parent
    return removed


def apply(
    preset: Annotated[Optional[str], typer.Argument()] = None,
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Target specific agent (e.g. universal, claude, cursor, all).")
    ] = None,
    copy: Annotated[
        bool,
        typer.Option("--copy", "-c", help="Copy skill files instead of creating symlinks.")
    ] = False,
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Target project directory (default: current directory).")
    ] = None,
    skill: Annotated[
        Optional[str],
        typer.Option("--skill", "-s", help="Only apply these skills (comma-separated). Omit it to pick from a list.")
    ] = None,
    npx: Annotated[
        bool,
        typer.Option("--npx", help="Install through `npx skills`, which copies files, writes skills-lock.json, and picks the skills.")
    ] = False,
    resync: Annotated[
        bool,
        typer.Option("--resync", help="Re-apply what this project already has, asking nothing.")
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Replace real directories in the way, not only this preset's links.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview the changes without writing files.")
    ] = False,
    pick_skills: Annotated[
        bool,
        typer.Option("--pick", "-i", help="Choose the skills in fzf, with SKILL.md in a preview pane.")
    ] = False,
    all_presets: Annotated[
        bool,
        typer.Option("--all-presets", "-A", help="Choose from every preset's skills at once.")
    ] = False,
) -> None:
    """Link preset skills into the project's agent directories.

    Symlinks by default, so the project follows later edits to the preset. A
    skill directory that is neither a link nor a copy this preset made is left
    alone.

    Naming a preset applies all of its skills without asking. A bare `skctl
    apply` asks which ones, in fzf if it is installed, otherwise a numbered
    list. `prompts.picker` in config.yaml fixes that choice.
    """
    config = load_config()
    target_preset = preset or config.get("default_preset", "default")
    use_copy = copy or (config.get("apply_mode") == "copy")
    target_project = Path(project).expanduser().resolve() if project else Path.cwd()
    recorded = (
        read_project_file(target_project) or load_applied().get(str(target_project), {})
        if dry_run else presets_for(target_project)
    )

    if resync:
        resync_project(target_project, recorded, dry_run=dry_run)
        return

    interactive = sys.stdin.isatty() and config.get("prompts", {}).get("ask_apply", True)

    # Which preset is always ours to ask; npx has no concept of them.
    if preset is None and interactive:
        available = sorted(d.name for d in PRESETS_DIR.iterdir() if d.is_dir()) if PRESETS_DIR.is_dir() else []
        # What this project already uses is the likeliest answer, so offer it first.
        if len(recorded) == 1:
            target_preset = next(iter(recorded))
        if len(available) > 1:
            target_preset = prompt_preset(available, target_project, PRESETS_DIR)

    # "All of them", from the preset prompt or --all-presets: the question becomes
    # which skills, across every preset at once, and each preset's share is applied
    # by an ordinary apply.
    if target_preset == ALL_PRESETS or (all_presets and preset is None):
        apply_across_presets(target_project, config, agent, use_copy, force, pick_skills, dry_run)
        return

    # Which skills is ours only for the symlink path. With --npx the selection is
    # left to `npx skills`, so don't ask here and don't send it a --skill filter.
    # Naming the preset on the command line is itself a choice: it applies every
    # skill in it with no further question, exactly like `-s` with nothing typed.
    # Only a bare `apply` (no preset named) or an explicit --pick still asks.
    if skill is None and not npx and (pick_skills or (preset is None and interactive)):
        skill = SELECT_INTERACTIVELY

    preset_dir = preset_path(target_preset)
    if not preset_dir.exists():
        if preset is not None:
            print_error(f"Preset '{target_preset}' not found at {preset_dir}")
            print_warn(f"You can create it by adding a skill: skctl add <package> --preset {target_preset}")
            sys.exit(1)
        # Nobody named a preset, so this is the default one on a machine that has
        # never used it: an empty preset, not a failure.
        if dry_run:
            print_warn(f"Preset '{target_preset}' does not exist; nothing to apply.")
            return
        preset_dir = ensure_preset_dir(target_preset)

    skills = get_preset_skills(preset_dir)
    if not skills:
        print_warn(f"Preset '{target_preset}' has no installed skills.")
        return

    if skill == SELECT_INTERACTIVELY:
        chosen = choose_skills(target_preset, skills, config, pick_skills, target_project)
        if not chosen:
            print_warn("Nothing picked.")
            return
    elif skill:
        chosen = [s.strip() for s in skill.split(",") if s.strip()]
    else:
        chosen = list(skills)

    unknown = [s for s in chosen if s not in skills]
    if unknown:
        print_error(f"Not in preset '{target_preset}': {', '.join(unknown)}")
        sys.exit(1)
    skills = {name: skills[name] for name in chosen}

    if npx:
        # Point npx at the directory that holds the skill folders, never at the
        # preset root: `npx skills add` ignores skills listed in a skills-lock.json
        # sitting beside them, and every preset has one naming exactly those skills.
        # Names must be passed as repeated --skill flags; a comma list matches nothing.
        filter_skills = skill is not None  # no -s means "let npx ask"
        if dry_run:
            print_header(f"Dry run: installing from '{target_preset}' via npx skills")
            for source in sorted({skills[name].parent for name in chosen}):
                args = ["add", str(source)]
                for name in chosen if filter_skills else []:
                    if skills[name].parent == source:
                        args.extend(["--skill", name])
                if agent:
                    args.extend(["--agent", "*" if agent == "all" else agent])
                if use_copy:
                    args.append("--copy")
                console.print(f"  Would run: npx skills {' '.join(shlex.quote(arg) for arg in args)}")
            return
        target_project.mkdir(parents=True, exist_ok=True)
        before = skill_kinds(target_project, project_skill_dirs(target_project, config), skills)
        code = 0
        for source in sorted({skills[name].parent for name in chosen}):
            args = ["add", str(source)]
            for name in chosen if filter_skills else []:
                if skills[name].parent == source:
                    args.extend(["--skill", name])
            if agent:
                args.extend(["--agent", "*" if agent == "all" else agent])
            if use_copy:
                args.append("--copy")
            print_header(f"Installing from '{target_preset}' via npx skills ({source})")
            code = run_npx_skills(args, cwd=str(target_project)) or code
        record_npx_result(target_project, target_preset, preset_dir, skills, before, config)
        sys.exit(code)

    agent_map = get_agent_dir_map(config)
    if agent in ("*", "all"):
        dest_rel_dirs = list(dict.fromkeys(agent_map.values()))
    elif agent:
        dest_rel_dirs = [agent_map.get(agent, f".{agent}/skills")]
    else:
        dest_rel_dirs = config.get("apply_targets", DEFAULT_TARGETS)

    owned = owned_copies(recorded, target_preset)
    if dry_run:
        preview_apply(target_project, target_preset, skills, dest_rel_dirs, use_copy, force, owned)
        return
    link_skills(target_project, target_preset, skills, dest_rel_dirs, use_copy, force=force, owned=owned)


def apply_across_presets(
    target_project: Path,
    config: dict,
    agent: Optional[str],
    use_copy: bool,
    force: bool,
    explicit_pick: bool,
    dry_run: bool = False,
) -> None:
    """Choose skills from every preset at once, then apply each preset's share.

    The preset a skill lives in is filing, not something you should have to remember
    before you can ask for it. Applying goes back through `apply` per preset, so
    every guard and the record behave exactly as they do otherwise.
    """
    rows = []
    for directory in sorted(d for d in PRESETS_DIR.iterdir() if d.is_dir()) if PRESETS_DIR.is_dir() else []:
        rows.extend(rows_for(directory.name, get_preset_skills(directory)))
    if not rows:
        print_warn("No skills in any preset yet.")
        console.print("Add one with: [header]skctl add <package> --preset <name>[/header]")
        return

    print_header(f"{len(rows)} skills across {len({row['preset'] for row in rows})} presets")
    header = (
        f"\x1b[1;36mAll presets\x1b[0m\n"
        f"\x1b[2mApplying to\x1b[0m  \x1b[2;32m{target_project}\x1b[0m\n"
        f"{DEFAULT_HEADER}"
    )
    chosen = pick(rows, header=header) if wants_picker(config, explicit_pick) \
        else pick_from_labels(rows)
    if not chosen:
        print_warn("Nothing picked.")
        return

    by_preset = {}
    for row in chosen:
        by_preset.setdefault(row["preset"], []).append(row["skill"])
    for preset, names in sorted(by_preset.items()):
        apply(
            preset=preset, skill=",".join(sorted(names)), project=str(target_project),
            agent=agent, copy=use_copy, force=force, dry_run=dry_run,
        )


def pick_from_labels(rows: list) -> list:
    """The numbered fallback when the rows span presets, so names need qualifying."""
    labels = [f"{row.get('location', row['preset'])}/{row['skill']}" for row in rows]
    chosen = set(prompt_skills(labels))
    return [row for row, label in zip(rows, labels) if label in chosen]


def add_installed_to_preset(
    name: str,
    preset_dir: Path,
    config: dict,
    confirm: bool = False,
    warn_empty: bool = True,
) -> int:
    """Pick installed skills and add them to a preset.

    Candidates come from other presets and configured global folders.
    Preset skills are copied. Tracked globals are reinstalled to retain their
    update metadata, while untracked globals are copied.
    """
    if not sys.stdin.isatty():
        print_error("Interactive preset selection requires a terminal.")
        return 0

    candidates = []
    for other in sorted(d for d in PRESETS_DIR.iterdir() if d.is_dir() and d != preset_dir):
        candidates.extend(rows_for(other.name, get_preset_skills(other)))
    candidates.extend(get_global_skills(config))
    present = set(get_preset_skills(preset_dir))
    candidates = [row for row in candidates if row["skill"] not in present]
    if not candidates:
        if warn_empty:
            print_warn(f"No installed skills are available to add to '{name}'.")
        return 0

    if confirm:
        console.print()
        try:
            if not Confirm.ask(
                f"Add skills to '{name}' now, from your presets and global folders?",
                default=False, console=console,
            ):
                return 0
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0

    chosen = pick(candidates, header=f"Add to '{name}' · {DEFAULT_HEADER}") if wants_picker(config) \
        else pick_from_labels(candidates)
    if not chosen:
        print_warn("Nothing picked; the preset stays empty.")
        return 0

    added = 0
    pending = list(chosen)
    lock = read_global_lock()
    tracked = {}
    for row in chosen:
        entry = lock.get(row["skill"]) if row.get("scope") == "global" else None
        source = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(source, str) and source:
            tracked.setdefault(source, []).append(row)

    # Reinstall tracked globals through upstream so the new preset receives its
    # own skills-lock.json and `skctl update --preset` can update them later.
    for source, rows in tracked.items():
        args = ["add", source]
        for row in rows:
            args.extend(["--skill", row["skill"]])
        args.extend(["--agent", "universal", "-y"])
        if run_npx_skills(args, cwd=str(preset_dir)) == 0:
            installed = {
                row["skill"] for row in rows
                if (preset_dir / ".agents" / "skills" / row["skill"]).is_dir()
            }
            added += len(installed)
            pending = [row for row in pending if row not in rows or row["skill"] not in installed]
        else:
            print_warn(f"Could not reinstall skills from {source}; copying them instead.")

    copied_without_lock = []
    for row in pending:
        dst = preset_dir / ".agents" / "skills" / row["skill"]
        if dst.exists():
            print_warn(f"Skipping {row['skill']} (already in '{name}')")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(row["path"], dst)
        added += 1
        if row.get("scope") == "global":
            copied_without_lock.append(row["skill"])

    if copied_without_lock:
        print_warn(
            "Copied global skills without update metadata; `update --preset` cannot update: "
            + ", ".join(sorted(copied_without_lock))
        )
    if added:
        names = ", ".join(sorted({row["skill"] for row in chosen}))
        print_success(f"Added {added} skill{'s' if added != 1 else ''} to '{name}': {names}")
    return added


def offer_add_after_create(name: str, preset_dir: Path, config: dict) -> None:
    """Offer installed or skills.sh additions after making a preset."""
    if not sys.stdin.isatty() or not config.get("prompts", {}).get("ask_add_after_create", True):
        return
    console.print()
    console.print(f"[dim]Add more skills to '{name}'?[/dim]")
    console.print("  [choice]1)[/choice] Pick from installed presets or global folders")
    console.print("  [choice]2)[/choice] Find on skills.sh")
    console.print("  [choice]3)[/choice] No, Done")
    try:
        choice = Prompt.ask("Choose an option", choices=["1", "2", "3"], default="3", console=console)
    except (EOFError, KeyboardInterrupt):
        console.print()
        return
    if choice == "1":
        add_installed_to_preset(name, preset_dir, config, warn_empty=False)
    elif choice == "2":
        from skill_ctl.skills import find

        find(preset=name)


def choose_skills(
    preset: str, skills: dict, config: dict, explicit_pick: bool, target_project: Path,
) -> list:
    """Ask which of a preset's skills to apply.

    fzf when it can run, so you can read a SKILL.md in the preview pane before
    taking it; the numbered list otherwise, which is all a pipe or a bare terminal
    can offer.
    """
    if wants_picker(config, explicit_pick):
        header = (
            f"\x1b[1;36m{preset}\x1b[0m\n"
            f"\x1b[2mApplying to\x1b[0m  \x1b[2;32m{target_project}\x1b[0m\n"
            f"{DEFAULT_HEADER}"
        )
        return [row["skill"] for row in pick(rows_for(preset, skills), header)]
    return prompt_skills(sorted(skills))


def project_skill_dirs(project: Path, config: dict) -> list:
    """Every directory in this project that could hold skills.

    The agent directories we know, plus any dot-directory holding a `skills/` that
    is actually there. `npx skills --agent goose` installs into `.goose/skills`, and
    upstream knows a dozen agents skctl does not and adds more; anything installed
    from a preset has to stay findable, whoever put the directory there.
    """
    found = list(all_target_dirs(config))
    for pattern in (".*/skills", ".*/*/skills"):
        for entry in sorted(project.glob(pattern)):
            rel = entry.relative_to(project).as_posix()
            if entry.is_dir() and rel not in found:
                found.append(rel)
    return found


def _legacy_skill_kinds(project: Path, rel_dirs: list, names: Iterable[str]) -> dict:
    """What sits at each (directory, skill) right now: a link, a real file, nothing."""
    kinds = {}
    for rel_dir in rel_dirs:
        for name in names:
            path = project / rel_dir / name
            if path.is_symlink():
                kinds[(rel_dir, name)] = "link"
            elif path.exists():
                kinds[(rel_dir, name)] = "real"
    return kinds


def record_npx_result(
    project: Path,
    preset: str,
    preset_dir: Path,
    skills: dict[str, Path],
    before: dict,
    config: dict,
) -> None:
    """Record what `npx skills` installed, by comparing the project with `before`.

    npx picks the skills and the agent directories itself and may link or copy, so
    there is nothing to write down until it has run. Anything that appeared, or
    changed from a link to a directory or back, came from this run; a skill that was
    already sitting there untouched is not this preset's to claim.

    The directories are looked up again here, because the one npx chose may not have
    existed when the run started.
    """
    after = skill_kinds(project, project_skill_dirs(project, config), skills)
    landed = {}
    targets = []
    copies = False
    for (rel_dir, name), kind in sorted(after.items()):
        path = project / rel_dir / name
        unchanged = before.get((rel_dir, name)) == kind
        if unchanged and not links_into_preset(path, preset_dir):
            continue
        landed[name] = skills[name]
        if rel_dir not in targets:
            targets.append(rel_dir)
        copies = copies or kind != "link"

    if not landed:
        return
    # `unapply` reads this to find them again, and a copy still needs --force,
    # exactly as one made by `apply --copy` does.
    record_apply(project, preset, landed, targets, "copy" if copies else "symlink")
    print_success(f"Recorded {len(landed)} skill{'s' if len(landed) != 1 else ''} from '{preset}' in {PROJECT_FILE}")


def _legacy_owned_copies(recorded: dict, preset: str) -> set:
    """Skill names this project already holds as copies of the given preset.

    A directory that is not a link belongs to whoever put it there, so `apply`
    leaves it alone - except when it is a copy `apply` itself made, which it must
    overwrite or a second `apply --copy` would update nothing.
    """
    entry = recorded.get(preset) or {}
    if entry.get("mode") != "copy":
        return set()
    return set(entry.get("skills", []))


def _legacy_link_skills(
    target_project: Path,
    target_preset: str,
    skills: dict[str, Path],
    dest_rel_dirs: list,
    use_copy: bool,
    force: bool = False,
    owned: Optional[set] = None,
) -> None:
    """Put one preset's skills into a project, and record that it happened."""
    from rich.progress import track

    print_header(f"Applying preset '{target_preset}' ({len(skills)} skills) to {target_project}")

    owned = owned or set()
    items = sorted(skills.items())
    # One line per skill, not per skill per directory. Past a few dozen even that
    # scrolls the useful output away, so those get a progress bar and a summary.
    per_skill = len(items) <= LIST_LIMIT
    linked = 0
    kept = 0
    applied: dict[str, Path] = {}

    for skill_name, skill_src in track(
        items, description="Linking skills", console=console,
        transient=True, disable=per_skill,
    ):
        done = []
        for rel_dir in dest_rel_dirs:
            target_dir = target_project / rel_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            dst = target_dir / skill_name

            if dst.is_symlink():
                # A link is cheap to replace and the preset is the newer answer.
                dst.unlink()
            elif dst.exists():
                # Real files are someone's work: a skill installed straight into
                # the project, or edited in place. Only our own copy, or an
                # explicit --force, may be thrown away.
                if not (force or skill_name in owned):
                    print_warn(f"Kept existing {rel_dir}/{skill_name} (not a link; --force replaces it)")
                    kept += 1
                    continue
                print_warn(f"Replacing existing {rel_dir}/{skill_name}")
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()

            if not use_copy:
                # Link to the path inside the preset, not to what the preset skill
                # itself may point at, so the link stays attributable to the preset
                # (and keeps tracking the preset if its own source is repointed).
                try:
                    dst.symlink_to(skill_src.absolute())
                    done.append(rel_dir)
                    continue
                except OSError as e:
                    # Windows refuses symlinks without Developer Mode or elevation.
                    print_warn(f"Cannot create symlinks ({e.strerror}); copying instead.")
                    use_copy = True  # warn once, copy the rest

            shutil.copytree(skill_src, dst)
            done.append(rel_dir)

        if done:
            linked += 1
            applied[skill_name] = skill_src
        if per_skill and done:
            print_success(f"{skill_name} -> {', '.join(done)}")

    verb = "Copied" if use_copy else "Linked"
    print_success(f"{verb} {linked} skill{'s' if linked != 1 else ''} into {', '.join(dest_rel_dirs)}")
    if kept:
        print_warn(f"Left {kept} existing director{'ies' if kept != 1 else 'y'} in place; --force replaces them.")
    # Only what actually landed is recorded: `unapply` and --resync act on this,
    # and neither may touch a directory this run refused to overwrite.
    if applied:
        record_apply(target_project, target_preset, applied, dest_rel_dirs, "copy" if use_copy else "symlink")


def link_skills(
    target_project: Path,
    target_preset: str,
    skills: dict[str, Path],
    dest_rel_dirs: list,
    use_copy: bool,
    force: bool = False,
    owned: Optional[set] = None,
) -> None:
    """Present and record the result of the filesystem linker."""
    print_header(f"Applying preset '{target_preset}' ({len(skills)} skills) to {target_project}")
    result = apply_skills(target_project, skills, dest_rel_dirs, use_copy, force, owned)
    for path in result.kept:
        print_warn(f"Kept existing {path} (not a link; --force replaces it)")
    verb = "Copied" if result.copied else "Linked"
    print_success(f"{verb} {len(result.applied)} skill{'s' if len(result.applied) != 1 else ''} into {', '.join(dest_rel_dirs)}")
    if result.applied:
        record_apply(target_project, target_preset, result.applied, dest_rel_dirs, "copy" if result.copied else "symlink")


def resync_project(target_project: Path, recorded: dict, dry_run: bool = False) -> None:
    """Re-apply exactly what the project already has, after the presets changed."""
    if not recorded:
        print_warn(f"No preset is recorded as applied to {target_project}.")
        console.print("Apply one first: [header]skctl apply <preset>[/header]")
        return

    for preset_name, entry in sorted(recorded.items()):
        preset_dir = preset_path(preset_name)
        if not preset_dir.exists():
            print_warn(f"Preset '{preset_name}' is gone; skipping. Clean up with: skctl doctor --fix")
            continue
        available = get_preset_skills(preset_dir)
        # A skill dropped from the preset since is simply no longer applied; a new
        # one is not added, because the record says which skills this project chose.
        wanted = {name: available[name] for name in entry.get("skills", []) if name in available}
        missing = [name for name in entry.get("skills", []) if name not in available]
        if missing:
            print_warn(f"No longer in '{preset_name}': {', '.join(missing)}")
        if not wanted:
            continue
        targets = entry.get("targets") or list(DEFAULT_TARGETS)
        use_copy = entry.get("mode") == "copy"
        owned = owned_copies(recorded, preset_name)
        if dry_run:
            preview_apply(target_project, preset_name, wanted, targets, use_copy, False, owned)
        else:
            link_skills(target_project, preset_name, wanted, targets, use_copy, owned=owned)


def unapply(
    preset: Annotated[Optional[str], typer.Argument()] = None,
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Detach from one agent only (e.g. universal, claude, cursor, or 'all').")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Target project directory (default: current directory).")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Also delete real directories, such as those --copy made.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Remove a preset's skills from the project.

    With no preset named, it removes what `apply` recorded for this project, or
    the default preset if there is no record. It deletes only what the preset
    put there, then removes the agent directories it emptied and its entries in
    skills-lock.json.
    """
    config = load_config()
    target_project = Path(project).expanduser().resolve() if project else Path.cwd()

    if preset:
        chosen_presets = [preset]
    else:
        recorded = presets_for(target_project)
        chosen_presets = sorted(recorded) or [config.get("default_preset", "default")]

    if not yes and sys.stdin.isatty():
        label = ", ".join(chosen_presets)
        try:
            confirmed = Confirm.ask(
                f"Remove preset{'s' if len(chosen_presets) != 1 else ''} '{label}' from {target_project}?",
                default=False, console=console,
            )
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not confirmed:
            return

    for name in chosen_presets:
        unapply_one(name, target_project, agent, force, config, named=bool(preset))


def unapply_one(
    target_preset: str,
    target_project: Path,
    agent: Optional[str],
    force: bool,
    config: dict,
    named: bool = True,
) -> None:
    preset_dir = preset_path(target_preset)
    entry = presets_for(target_project).get(target_preset) or {}
    # What `apply` put here, which is not the same as what the preset holds now:
    # a skill dropped from the preset since still has a copy or a link in the
    # project, and only the record can still name it.
    recorded_names = set(entry.get("skills", []))
    copied = entry.get("mode") == "copy"

    if not preset_dir.exists():
        if not entry:
            # Only worth an error when the user picked this preset themselves; the
            # fallback to the default one landing on an absent preset means there
            # is simply nothing applied here.
            if named:
                print_error(f"Preset '{target_preset}' not found at {preset_dir}")
                sys.exit(1)
            print_warn(f"Nothing from preset '{target_preset}' is applied to {target_project}.")
            return
        print_warn(f"Preset '{target_preset}' is gone; removing what it left here.")

    skills = get_preset_skills(preset_dir) if preset_dir.exists() else {}
    names = sorted(set(skills) | recorded_names)
    if not names:
        print_warn(f"No skills recorded in preset '{target_preset}'.")
        return

    agent_map = get_agent_dir_map(config)
    if agent and agent not in ("*", "all"):
        target_dirs = [agent_map.get(agent, f".{agent}/skills")]
    else:
        # No agent, or '*'/'all' as accepted by `apply`: sweep every directory this
        # project actually has, plus any the record names that are already gone.
        target_dirs = list(dict.fromkeys(
            project_skill_dirs(target_project, config) + list(entry.get("targets", []))
        ))

    print_header(f"Removing preset '{target_preset}' skills from project at {target_project}")
    removed = 0
    skipped = 0
    emptied = set()
    removed_names = set()
    left_behind = set()
    per_skill = len(names) <= LIST_LIMIT

    from rich.progress import track

    for skill_name in track(
        names, description="Removing skills", console=console,
        transient=True, disable=per_skill,
    ):
        gone = []
        for rel_dir in target_dirs:
            target = target_project / rel_dir / skill_name
            if not (target.is_symlink() or target.exists()):
                continue

            # Only delete what this preset put here. A same-named skill installed
            # directly, or linked from another preset, is not ours to remove.
            # The second test catches links written by older versions, which
            # pointed at whatever the preset's own skill resolved to. The third
            # A copy is not a link and so never counts as ours: --force removes
            # those, as it always has, and naming them from the record is what
            # lets it reach a copy of a skill the preset no longer holds.
            source = skills.get(skill_name)
            ours = links_into_preset(target, preset_dir) or (
                source is not None and target.is_symlink()
                and target.resolve() == source.resolve()
            )
            if not (ours or force):
                why = (
                    f"a copy from '{target_preset}'" if copied and skill_name in recorded_names
                    else f"not a link into '{target_preset}'"
                )
                print_warn(f"Skipping {rel_dir}/{skill_name} ({why}; use --force)")
                skipped += 1
                left_behind.add(skill_name)
                continue

            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
            gone.append(rel_dir)
            emptied.add(target.parent)
            removed_names.add(skill_name)
            removed += 1

        if per_skill and gone:
            print_success(f"{skill_name} removed from {', '.join(gone)}")

    # Don't leave the agent directories behind as empty shells.
    for directory in emptied:
        pruned = prune_empty_dirs(directory, target_project)
        if pruned:
            print_success(f"Removed empty {pruned.relative_to(target_project)}/")

    unlocked = prune_lockfile(target_project, removed_names)
    if unlocked:
        print_success(f"Dropped {unlocked} entries from skills-lock.json")

    if left_behind:
        # The record is the only thing that can still name what stayed - a copy of
        # a skill the preset has since dropped has nothing else pointing at it - so
        # keep the entry, naming just those, until a --force run clears them.
        keep_only(target_project, target_preset, left_behind)
    else:
        forget_apply(target_project, target_preset)
    print_header(f"Unapplied preset '{target_preset}' ({removed} removed, {skipped} skipped).")

def _show_preset(name: str) -> None:
    from rich import box
    from rich.table import Table

    p = preset_path(name)
    if not p.exists():
        print_error(f"Preset '{name}' does not exist.")
        sys.exit(1)

    skills = sorted(get_preset_skills(p))
    print_header(f"Preset '{name}'")
    console.print(f"  Location: {p}")
    console.print(f"  Skills: {len(skills)}")
    console.print(f"  {', '.join(skills) if skills else '[dim](none)[/dim]'}")

    table = Table(title="Applied projects", box=box.ROUNDED, header_style="header")
    table.add_column("Project")
    table.add_column("Skills", justify="right")
    table.add_column("Mode")
    table.add_column("Targets")
    rows = [
        (path, entry)
        for path, presets_data in load_applied().items()
        for preset_name, entry in presets_data.items()
        if preset_name == name
    ]
    if not rows:
        console.print("[dim]Not applied to any recorded project.[/dim]")
        return
    for path, entry in sorted(rows):
        table.add_row(
            path,
            str(len(entry.get("skills", []))),
            str(entry.get("mode", "symlink")),
            ", ".join(entry.get("targets", [])) or "-",
        )
    console.print(table)


def browse_presets(preset_dirs: list[Path]) -> tuple[Optional[str], list[str]]:
    """Browse presets and return an fzf action plus the selected names."""
    config = load_config()
    if not wants_picker(config):
        return None, []

    applied = load_applied()
    colors = picker_ansi(config.get("theme"))
    reset = colors["reset"]
    with tempfile.TemporaryDirectory(prefix="skctl-presets-") as temporary:
        root = Path(temporary)
        rows = []
        for index, preset_dir in enumerate(preset_dirs, 1):
            skills = sorted(get_preset_skills(preset_dir))
            projects = sorted(
                path for path, data in applied.items() if preset_dir.name in data
            )
            preview = root / str(index)
            preview.write_text(preset_preview(preset_dir, projects), encoding="utf-8")
            rows.append("\t".join((
                str(index),
                f"{colors['name']}{preset_dir.name:<22}{reset}",
                f"{colors['description']}{len(skills):>6}{reset}",
                f"{colors['location']}{len(projects):>6}{reset}",
                str(preview),
            )))

        preview_command = f"type {{5}}" if os.name == "nt" else "cat {5}"
        result = subprocess.run(
            [
                "fzf", "--ansi", "--multi", "--expect", "alt-x,alt-e,alt-r,alt-n,alt-y,alt-m", "--tabstop", "2",
                "--delimiter", "\t", "--with-nth", "2,3,4",
                "--header", "Preset                  Skills  Applied projects\nTab select · Enter info · Alt-N new · Alt-Y clone · Alt-M combine · Alt-R rename · Alt-X delete · Alt-E export",
                "--color", fzf_color_arg(config.get("theme")),
                "--preview", preview_command, "--preview-window", "right,55%,wrap",
            ],
            input="\n".join(rows), stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None, []
        selected = result.stdout.splitlines()
        action = selected.pop(0) if selected[0] in ("alt-x", "alt-e", "alt-r", "alt-n", "alt-y", "alt-m") else None
        if selected and not selected[0]:
            selected.pop(0)
        names = [preset_dirs[int(row.split("\t", 1)[0]) - 1].name for row in selected]
        return action, names


def _print_preset_summaries(names: list[str]) -> None:
    for name in names:
        preset_dir = preset_path(name)
        skills = sorted(get_preset_skills(preset_dir))
        console.print(f"[header]{name}[/header] [dim]{len(skills)} skills · {len(projects_using(name))} applied projects[/dim]")
        console.print(f"  [path]{preset_dir}[/path]")
        console.print(f"  {', '.join(skills) if skills else '[dim](none)[/dim]'}")


def presets(
    action: Annotated[Literal["browse", "list", "applied", "create", "clone", "combine", "history", "rename", "delete", "export", "import"], typer.Argument()] = "browse",
    name: Annotated[str, typer.Argument()] = "",
    new_name: Annotated[str, typer.Argument()] = "",
    source_names: Annotated[list[str], typer.Argument()] = [],
    from_path: Annotated[Optional[str], typer.Option("--from", help="Copy skills from this path when creating a preset.")] = None,
    clean: Annotated[bool, typer.Option("--clean")] = False,
    output: Annotated[Optional[str], typer.Option("--output", "-o", help="Output path for `presets export`.")] = None,
    import_rename: Annotated[Optional[str], typer.Option("--rename", help="New name for an imported preset.")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Replace an existing preset when importing, cloning, or combining.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview an import, clone, or combine without writing files.")] = False,
) -> None:
    """Create, clone, combine, rename, delete, export, and import presets.

    Examples: `skctl presets clone BASE NEW`,
    `skctl presets combine DEV FRONTEND BACKEND`,
    `skctl presets history DEV`,
    `skctl presets export NAME -o ARCHIVE`,
    `skctl presets import ARCHIVE --rename NAME`.

    Deleting the default_preset leaves an empty one behind, because `apply` and
    the add prompt both fall back to it.
    """
    from rich import box
    from rich.table import Table

    if from_path and action != "create":
        print_error("--from is only available with `skctl presets create`.")
        sys.exit(1)
    if action == "clone":
        if not name or not new_name:
            print_error("Error: Usage: skctl presets clone <source> <new-name> [--replace]")
            sys.exit(1)
        clone_preset(name, new_name, replace, dry_run)
        return
    if action == "combine":
        sources = [new_name, *source_names] if new_name else source_names
        if not name:
            print_error("Error: Usage: skctl presets combine <new-name> <preset> <preset> [...]")
            sys.exit(1)
        combine_presets(name, sources, replace, dry_run)
        return
    if action == "history":
        if not name:
            print_error("Error: Usage: skctl presets history <name>")
            sys.exit(1)
        preset_history(name)
        return
    if action == "export":
        if not name:
            print_error("Error: Usage: skctl presets export <name> [--output <archive>]")
            sys.exit(1)
        preset_export(name, output)
        return
    if action == "import":
        if not name:
            print_error("Error: Usage: skctl presets import <archive> [--rename <name>] [--replace]")
            sys.exit(1)
        preset_import(name, import_rename, replace, dry_run)
        return

    config = load_config()
    default_preset = config.get("default_preset", "default")
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    # `apply` and the add prompt both fall back to the default preset, so this is
    # where it comes into being: reading the config no longer creates directories.
    ensure_preset_dir(default_preset)

    preset_dirs = sorted(d for d in PRESETS_DIR.iterdir() if d.is_dir())
    if action == "browse":
        picker_action, picked = browse_presets(preset_dirs)
        if picker_action == "alt-n":
            presets("create", prompt_new_preset_name())
            return
        if picked:
            if picker_action == "alt-x":
                if Confirm.ask(f"Delete preset(s): {', '.join(picked)}?", default=False):
                    for preset_name in picked:
                        presets("delete", preset_name)
            elif picker_action == "alt-e":
                if Confirm.ask(f"Export preset(s): {', '.join(picked)}?", default=False):
                    for preset_name in picked:
                        preset_export(preset_name)
            elif picker_action == "alt-r":
                if len(picked) != 1:
                    print_warn("Select one preset to rename.")
                else:
                    presets("rename", picked[0], prompt_new_preset_name())
            elif picker_action == "alt-y":
                if len(picked) != 1:
                    print_warn("Select one preset to clone.")
                else:
                    clone_preset(picked[0], prompt_new_preset_name())
            elif picker_action == "alt-m":
                if len(picked) < 2:
                    print_warn("Select at least two presets to combine.")
                else:
                    combine_presets(prompt_new_preset_name(), picked)
            else:
                _print_preset_summaries(picked)
            return
        if wants_picker(config):
            return
        action = "list"

    if action == "list":
        if not preset_dirs:
            console.print("[dim]No presets found in ~/.skill-ctl/presets/[/dim]")
            console.print("Create one with: [header]skctl add <package> --preset <name>[/header]")
            return

        table = Table(title="[header]Presets in ~/.skill-ctl/presets/[/header]", box=box.ROUNDED, header_style="header")
        table.add_column("Preset", style="choice")
        table.add_column("Skills", justify="right")
        table.add_column("Applied in", justify="right")
        table.add_column("Installed Skills", style="path")

        for p in preset_dirs:
            skills = get_preset_skills(p)
            names = sorted(skills)
            # A preset with hundreds of skills would push the table off screen.
            if len(names) > TABLE_LIMIT:
                shown = f"{', '.join(names[:TABLE_LIMIT])} [dim]+{len(names) - TABLE_LIMIT} more[/dim]"
            else:
                shown = ", ".join(names) if names else "[dim](none)[/dim]"
            users = len(projects_using(p.name))
            marker = f"[bold]{users}[/bold]" if users else "[dim]-[/dim]"
            table.add_row(p.name, str(len(skills)), marker, shown)

        console.print(table)
        console.print("[dim]Applied in: projects `skctl apply` has linked this preset into.[/dim]")

    elif action == "applied":
        table = Table(title="Applied projects", box=box.ROUNDED, header_style="header")
        table.add_column("Project")
        table.add_column("Preset", style="choice")
        table.add_column("Skills", justify="right")
        table.add_column("Mode")
        table.add_column("Targets")
        applied = load_applied()
        stale = [path for path in applied if not Path(path).is_dir()]
        if clean and stale:
            for path in stale:
                print_warn(f"Removing missing project from applied index: {path}")
                applied.pop(path, None)
            save_applied(applied)
        rows = [
            (path, preset_name, entry)
            for path, presets_data in applied.items()
            for preset_name, entry in presets_data.items()
        ]
        if not rows:
            console.print("[dim]No applied projects recorded yet.[/dim]")
            return
        for path, preset_name, entry in sorted(rows):
            table.add_row(
                path,
                preset_name,
                str(len(entry.get("skills", []))),
                str(entry.get("mode", "symlink")),
                ", ".join(entry.get("targets", [])) or "-",
            )
        console.print(table)

    elif action == "create":
        if not name:
            print_error("Error: Preset name is required. Usage: skctl presets create <name>")
            sys.exit(1)
        # Ask about filling it only when it is genuinely new - re-running `create`
        # on one that already has skills must not re-open that question.
        is_new = not preset_path(name).exists()
        p = ensure_preset_dir(name)
        print_success(f"Created preset '{name}' at {p}")
        if from_path:
            copy_skills_from_path(from_path, p)
        if is_new:
            offer_add_after_create(name, p, config)
        maybe_auto_push(f"create preset {name}")

    elif action == "rename":
        if not name or not new_name:
            print_error("Error: Usage: skctl presets rename <old-name> <new-name>")
            sys.exit(1)
        old_dir = preset_path(name)
        new_dir = preset_path(new_name)
        if not old_dir.exists():
            print_error(f"Preset '{name}' does not exist.")
            sys.exit(1)
        if new_dir.exists():
            print_error(f"Preset '{new_name}' already exists.")
            sys.exit(1)
        old_dir.rename(new_dir)

        applied = load_applied()
        for path, presets_data in list(applied.items()):
            if name not in presets_data:
                continue
            presets_data[new_name] = presets_data.pop(name)
            project = Path(path)
            if project.is_dir():
                project_data = read_project_file(project)
                if name in project_data:
                    project_data[new_name] = project_data.pop(name)
                    write_project_file(project, project_data)
        save_applied(applied)

        if name == default_preset and CONFIG_FILE.exists():
            text = CONFIG_FILE.read_text(encoding="utf-8")
            updated = re.sub(r"(?m)^(default_preset:\s*)[^\n]+$", rf"\g<1>{new_name}", text, count=1)
            if updated != text:
                CONFIG_FILE.write_text(updated, encoding="utf-8")
        print_success(f"Renamed preset '{name}' to '{new_name}'")
        maybe_auto_push(f"rename preset {name} to {new_name}")

    elif action == "delete":
        if not name:
            print_error("Error: Preset name is required. Usage: skctl presets delete <name>")
            sys.exit(1)
        p = preset_path(name)
        if not p.exists():
            print_error(f"Preset '{name}' does not exist.")
            sys.exit(1)

        # Deleting it strands every symlink pointing in here, so say where they are.
        users = projects_using(name)
        if users:
            print_warn(f"'{name}' is applied to {len(users)} project(s); their links will break:")
            for path in users[:10]:
                console.print(f"    {path}")
            if len(users) > 10:
                console.print(f"    ... and {len(users) - 10} more")
            console.print("Clean them up afterwards with: [header]skctl doctor --all --fix[/header]")

        shutil.rmtree(p)
        print_success(f"Deleted preset '{name}'")

        # The configured default is what `apply` and the destination prompt fall
        # back to, so leave an empty one behind rather than a dangling reference.
        if name == default_preset:
            ensure_preset_dir(name)
            print_warn(f"Recreated '{name}' empty (it is the default preset).")

        maybe_auto_push(f"delete preset {name}")


def status(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
) -> None:
    """Show presets recorded for a project and whether their skills are present."""
    from rich import box
    from rich.table import Table

    config = load_config()
    target = Path(project).expanduser().resolve() if project else Path.cwd()
    recorded = presets_for(target)
    print_header(f"Skill status for {target}")
    if recorded:
        table = Table(box=box.ROUNDED, header_style="header")
        table.add_column("Preset", style="choice")
        table.add_column("Skills", justify="right")
        table.add_column("Mode")
        table.add_column("State")
        dirs = project_skill_dirs(target, config)
        for name, entry in sorted(recorded.items()):
            names = set(entry.get("skills", []))
            preset_dir = preset_path(name)
            if not preset_dir.exists():
                state = "preset missing"
            else:
                missing = sum(
                    not any((target / rel_dir / skill).exists() for rel_dir in (entry.get("targets") or dirs))
                    for skill in names
                )
                state = "ok" if not missing else f"{missing} missing"
            table.add_row(name, str(len(names)), str(entry.get("mode", "symlink")), state)
        console.print(table)
    else:
        console.print("[dim]No presets recorded for this project.[/dim]")

    console.print("[dim]More detail: skctl presets applied, or browse presets with skctl presets[/dim]")
