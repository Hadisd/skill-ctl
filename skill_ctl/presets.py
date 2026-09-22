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
from skill_ctl.prompts import prompt_apply_destination, prompt_apply_type, prompt_new_preset_name, prompt_preset, prompt_skills
from skill_ctl.archive import export_preset as write_archive, safe_extract_preset, preset_changes
from skill_ctl.linker import apply_skills, links_into_preset, prune_lockfile, prune_empty_dirs, skill_kinds, owned_copies
from skill_ctl.picker import DEFAULT_HEADER, pick, rows_for, wants_picker
from skill_ctl.runner import run_npx_skills
# Re-exported: it lives in its own module so callers that only count skills
# need not import this one, but it stays reachable as presets.get_preset_skills.
from skill_ctl.skillscan import get_preset_skills
from skill_ctl.theme import bat_theme, fzf_color_arg, picker_ansi
from skill_ctl.registry import (
    PROJECT_FILE, load_applied, save_applied, read_project_file, write_project_file,
    record_apply, forget_apply, keep_only, presets_for, projects_using,
)
from skill_ctl.backup import maybe_auto_push


def preview_markdown_command(field_placeholder: str = "{5}") -> str:
    """Render markdown preview using bat or glow when available, falling back to cat/type."""
    width = '"%FZF_PREVIEW_COLUMNS%"' if os.name == "nt" else '"$FZF_PREVIEW_COLUMNS"'
    theme = shlex.quote(bat_theme(load_config().get("theme")))
    bat_options = (
        f'--color=always --paging=never --style=plain --language=md --theme={theme} '
        f'--squeeze-blank --wrap=character --terminal-width={width}'
    )
    if shutil.which("bat"):
        return f"bat {bat_options} {field_placeholder}"
    if shutil.which("batcat"):
        return f"batcat {bat_options} {field_placeholder}"
    if shutil.which("glow"):
        return f"glow --style dark {field_placeholder}"
    command = "type" if os.name == "nt" else "cat"
    return f"{command} {field_placeholder}"

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
    """Build the fzf Markdown preview for a preset, including its recorded origin."""
    skills = sorted(get_preset_skills(preset_dir))
    md = [
        f"# {preset_dir.name}",
        "",
        f"**Location:** `{preset_dir}`",
        "",
    ]
    if projects:
        md.append(f"### Applied Projects ({len(projects)})")
        for project in projects:
            md.append(f"- `{project}`")
        md.append("")
    else:
        md.append("### Applied Projects\n*(none)*\n")

    if skills:
        md.append(f"### Skills ({len(skills)})")
        for skill in skills:
            md.append(f"- **{skill}**")
        md.append("")
    else:
        md.append("### Skills\n*(none)*\n")

    events = _history_events(preset_dir)
    if events:
        md.append("### History:")
        for event in events:
            at = event.get('at', '')
            action = event.get('action', '')
            sources = ', '.join(event.get('sources', []))
            md.append(f"- `{at}` **{action}** {sources}")
        md.append("")

    return "\n".join(md).strip() + "\n"


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


def github_repo_url(repository: str) -> str:
    """Turn GitHub's owner/repository shorthand into a clone URL."""
    if "://" in repository or repository.startswith("git@") or Path(repository).exists():
        return repository
    if repository.count("/") == 1:
        return f"https://github.com/{repository}.git"
    print_error("GitHub repositories must be owner/repository or a clone URL.")
    sys.exit(1)


def remote_preset_preview(preset_dir: Path) -> str:
    """Show the useful parts of a downloaded preset without its temporary path."""
    skills = sorted(get_preset_skills(preset_dir))
    files = sorted(str(path.relative_to(preset_dir)) for path in preset_dir.rglob("*") if path.is_file())
    md = [
        f"# {preset_dir.name}",
        "",
    ]
    if skills:
        md.append(f"### Skills ({len(skills)})")
        for skill in skills:
            md.append(f"- **{skill}**")
        md.append("")
    else:
        md.append("### Skills\n*(none)*\n")

    if files:
        md.append(f"### Files ({len(files)})")
        for path in files:
            md.append(f"- `{path}`")
        md.append("")
    else:
        md.append("### Files\n*(none)*\n")

    return "\n".join(md).strip() + "\n"


def pick_remote_presets(preset_dirs: list[Path]) -> list[str]:
    """Pick downloaded presets with the same selection keys as other pickers."""
    config = load_config()
    if not wants_picker(config):
        return []
    colors = picker_ansi(config.get("theme"))
    reset = colors["reset"]
    with tempfile.TemporaryDirectory(prefix="skctl-remote-presets-") as temporary:
        root = Path(temporary)
        rows = []
        for index, preset_dir in enumerate(preset_dirs, 1):
            preview = root / str(index)
            preview.write_text(remote_preset_preview(preset_dir), encoding="utf-8")
            rows.append("\t".join((
                str(index),
                f"{colors['name']}{preset_dir.name:<22}{reset}",
                f"{colors['description']}{len(get_preset_skills(preset_dir)):>6}{reset}",
                str(preview),
            )))
        preview_command = preview_markdown_command("{4}")
        result = subprocess.run(
            [
                "fzf", "--ansi", "--multi", "--tabstop", "2", "--delimiter", "\t", "--with-nth", "2,3",
                "--header", "Preset                  Skills\nTab select · Ctrl-A all · Ctrl-D none · Enter import",
                "--color", fzf_color_arg(config.get("theme")),
                "--bind", "ctrl-a:select-all,ctrl-d:deselect-all",
                "--preview", preview_command, "--preview-window", "right,55%,wrap",
            ],
            input="\n".join(rows), stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return [preset_dirs[int(row.split("\t", 1)[0]) - 1].name for row in result.stdout.splitlines()]


def import_github_presets(
    repository: str,
    names: list[str],
    rename: Optional[str] = None,
    replace: bool = False,
    dry_run: bool = False,
) -> None:
    """Download a GitHub preset backup and import selected presets from it."""
    if rename and len(names) > 1:
        print_error("--rename can only be used with one --preset.")
        sys.exit(1)
    if shutil.which("git") is None:
        print_error("'git' not found on PATH. Install git and retry.")
        sys.exit(1)
    with tempfile.TemporaryDirectory(prefix="skctl-github-") as temporary:
        clone = Path(temporary) / "repository"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", github_repo_url(repository), str(clone)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            print_error((result.stderr or result.stdout).strip() or f"Could not download {repository}.")
            sys.exit(1)
        source_root = clone / "presets"
        available = sorted(path for path in source_root.iterdir() if path.is_dir()) if source_root.is_dir() else []
        if not available:
            print_error(f"No presets found in {repository}. Expected a presets/ directory.")
            sys.exit(1)
        available_by_name = {path.name: path for path in available}
        selected = names or pick_remote_presets(available)
        if not selected:
            if names:
                return
            print_warn("No preset selected. Pass --preset NAME when fzf is unavailable.")
            return
        missing = [name for name in selected if name not in available_by_name]
        if missing:
            print_error(f"Preset not found in {repository}: {', '.join(missing)}")
            sys.exit(1)
        for source_name in selected:
            target_name = rename or source_name
            target = preset_path(target_name)
            source = available_by_name[source_name]
            changes = preset_changes(target, source) if target.exists() else [
                ("add", path.relative_to(source)) for path in source.rglob("*") if path.is_file()
            ]
            print_header(f"Import preview: '{source_name}' -> '{target_name}'")
            for action, path in changes:
                console.print(f"  {action:6} {path}")
            if not changes:
                console.print("  [dim]no file changes[/dim]")
            if dry_run:
                continue
            if target.exists() and not replace:
                print_error(f"Preset '{target_name}' already exists. Use --replace or --rename <name>.")
                continue
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            print_success(f"Imported preset '{target_name}' from {repository}")
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
    global_apply: Annotated[
        bool,
        typer.Option("--global", "-g", help="Apply skills globally to user agent directories (~/.agents/skills, ~/.claude/skills, ...).")
    ] = False,
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
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt.")
    ] = False,
) -> None:
    """Link preset skills into the project's agent directories or globally.

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

    if global_apply:
        target_project = Path.home()
        is_global = True
    elif project:
        target_project = Path(project).expanduser().resolve()
        is_global = False
    else:
        target_project = Path.cwd()
        is_global = False

    recorded = (
        read_project_file(target_project) or load_applied().get(str(target_project), {})
        if dry_run else presets_for(target_project)
    )

    if resync:
        resync_project(target_project, recorded, dry_run=dry_run)
        return

    interactive = sys.stdin.isatty() and config.get("prompts", {}).get("ask_apply", True)

    if not project and not global_apply and interactive and preset is None and not resync:
        dest_choice = prompt_apply_destination(target_project)
        if dest_choice == "2":
            target_project = Path.home()
            is_global = True
            global_apply = True
            recorded = load_applied().get(str(target_project), {}) if dry_run else presets_for(target_project)

    applied_entire_preset = False
    # Which preset is always ours to ask; npx has no concept of them.
    if preset is None and interactive:
        available = sorted(d.name for d in PRESETS_DIR.iterdir() if d.is_dir()) if PRESETS_DIR.is_dir() else []
        if not available:
            print_warn("No presets found.")
            console.print("Create one with: [header]skctl add <package> --preset <name>[/header]")
            return

        if skill is None and not all_presets and not pick_skills:
            apply_type = prompt_apply_type()
            if apply_type == "1":
                apply_across_presets(target_project, config, agent, use_copy, force, explicit_pick=True, dry_run=dry_run, global_apply=is_global, yes=yes)
                return
            applied_entire_preset = True
            default_choice = next(iter(recorded)) if len(recorded) == 1 and next(iter(recorded)) in available else None
            if len(available) == 1:
                target_preset = available[0]
            else:
                target_preset = prompt_preset(available, target_project, PRESETS_DIR, include_all=False, default_name=default_choice)
        else:
            # What this project already uses is the likeliest answer, so offer it first.
            if len(recorded) == 1:
                target_preset = next(iter(recorded))
            if len(available) > 1:
                target_preset = prompt_preset(available, target_project, PRESETS_DIR)

    # "All of them", from the preset prompt or --all-presets: the question becomes
    # which skills, across every preset at once, and each preset's share is applied
    # by an ordinary apply.
    if target_preset == ALL_PRESETS or (all_presets and preset is None):
        apply_across_presets(target_project, config, agent, use_copy, force, pick_skills, dry_run, global_apply=is_global, yes=yes)
        return

    # Which skills is ours only for the symlink path. With --npx the selection is
    # left to `npx skills`, so don't ask here and don't send it a --skill filter.
    # Naming the preset on the command line is itself a choice: it applies every
    # skill in it with no further question, exactly like `-s` with nothing typed.
    # Only a bare `apply` (no preset named) or an explicit --pick still asks.
    if skill is None and not npx and (pick_skills or (preset is None and interactive and not applied_entire_preset)):
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

    if not yes and sys.stdin.isatty() and not dry_run:
        is_home = is_global or target_project == Path.home()
        dest_label = "Global (~/)" if is_home else str(target_project)
        skill_count = len(skills)
        if skill_count <= 4:
            names_str = ", ".join(sorted(skills.keys()))
            prompt_msg = f"Apply preset '{target_preset}' ({names_str}) to {dest_label}?"
        else:
            prompt_msg = f"Apply preset '{target_preset}' ({skill_count} skills) to {dest_label}?"
        try:
            confirmed = Confirm.ask(prompt_msg, default=True, console=console)
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not confirmed:
            return

    if npx:
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
                if is_global:
                    args.append("-g")
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
            if is_global:
                args.append("-g")
            if use_copy:
                args.append("--copy")
            print_header(f"Installing from '{target_preset}' via npx skills ({source})")
            code = run_npx_skills(args, cwd=str(target_project)) or code
        record_npx_result(target_project, target_preset, preset_dir, skills, before, config)
        sys.exit(code)

    agent_map = get_agent_dir_map(config)
    if is_global or target_project == Path.home():
        if agent in ("*", "all"):
            dest_rel_dirs = list(dict.fromkeys(agent_map.values()))
        elif agent:
            dest_rel_dirs = [agent_map.get(agent, f".{agent}/skills")]
        elif config.get("default_agents"):
            dest_rel_dirs = list(dict.fromkeys(agent_map.get(a, f".{a}/skills") for a in config["default_agents"]))
        else:
            from skill_ctl.runner import get_detected_global_agents
            dest_rel_dirs = list(dict.fromkeys(agent_map.get(a, f".{a}/skills") for a in get_detected_global_agents()))
    else:
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
    agent: Optional[str] = None,
    use_copy: bool = False,
    force: bool = False,
    explicit_pick: bool = True,
    dry_run: bool = False,
    skill: Optional[str] = None,
    global_apply: bool = False,
    yes: bool = False,
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

    is_home = global_apply or target_project == Path.home()
    dest_label = "Global (~/)" if is_home else str(target_project)

    if skill:
        wanted = {s.strip() for s in skill.split(",") if s.strip()}
        matched_rows = [row for row in rows if row["skill"] in wanted]
        if not matched_rows:
            print_warn(f"Skill '{skill}' not found in any preset.")
            return
        if len(matched_rows) == len(wanted):
            chosen = matched_rows
        elif sys.stdin.isatty() and wants_picker(config, explicit_pick):
            header = (
                f"\x1b[1;36mAll presets\x1b[0m\n"
                f"\x1b[2mApplying to\x1b[0m  \x1b[2;32m{dest_label}\x1b[0m\n"
                f"{DEFAULT_HEADER}"
            )
            chosen = pick(matched_rows, header=header)
        elif sys.stdin.isatty():
            chosen = pick_from_labels(matched_rows)
        else:
            seen = set()
            chosen = []
            for row in matched_rows:
                if row["skill"] not in seen:
                    seen.add(row["skill"])
                    chosen.append(row)
    else:
        print_header(f"{len(rows)} skills across {len({row['preset'] for row in rows})} presets")
        header = (
            f"\x1b[1;36mAll presets\x1b[0m\n"
            f"\x1b[2mApplying to\x1b[0m  \x1b[2;32m{dest_label}\x1b[0m\n"
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

    if not yes and sys.stdin.isatty() and not dry_run:
        prompt_msg = f"Apply {len(chosen)} skill{'s' if len(chosen) != 1 else ''} across {len(by_preset)} preset{'s' if len(by_preset) != 1 else ''} to {dest_label}?"
        try:
            confirmed = Confirm.ask(prompt_msg, default=True, console=console)
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not confirmed:
            return

    for preset, names in sorted(by_preset.items()):
        apply(
            preset=preset, skill=",".join(sorted(names)),
            project=str(target_project) if not is_home else None,
            global_apply=is_home,
            agent=agent, copy=use_copy, force=force, dry_run=dry_run, yes=True,
        )


def pick_from_labels(rows: list) -> list:
    """The numbered fallback when the rows span presets, so names need qualifying."""
    labels = [f"{row.get('location', row['preset'])}/{row['skill']}" for row in rows]
    chosen = set(prompt_skills(labels))
    return [row for row, label in zip(rows, labels) if label in chosen]


def install_skills_into_preset(name: str, preset_dir: Path, rows: list[dict]) -> int:
    """Install or copy candidate skill rows into a preset directory."""
    if not rows:
        return 0
    added = 0
    pending = list(rows)
    lock = read_global_lock()
    tracked = {}
    for row in rows:
        entry = lock.get(row["skill"]) if row.get("scope") == "global" else None
        source = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(source, str) and source:
            tracked.setdefault(source, []).append(row)

    # Reinstall tracked globals through upstream so the new preset receives its
    # own skills-lock.json and `skctl update --preset` can update them later.
    for source, source_rows in tracked.items():
        args = ["add", source]
        for row in source_rows:
            args.extend(["--skill", row["skill"]])
        args.extend(["--agent", "universal", "-y"])
        if run_npx_skills(args, cwd=str(preset_dir)) == 0:
            installed = {
                row["skill"] for row in source_rows
                if (preset_dir / ".agents" / "skills" / row["skill"]).is_dir()
            }
            added += len(installed)
            pending = [row for row in pending if row not in source_rows or row["skill"] not in installed]
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
        names = ", ".join(sorted({row["skill"] for row in rows}))
        print_success(f"Added {added} skill{'s' if added != 1 else ''} to '{name}': {names}")
    return added


def remove_skills_from_preset(preset_dir: Path, skill_names: Iterable[str]) -> list[str]:
    """Remove named skills from a preset directory and update its lockfile."""
    removed = []
    names_set = set(skill_names)
    for skill_name in names_set:
        found = False
        for pattern in (f"*/skills/{skill_name}", f"*/*/skills/{skill_name}", f"skills/{skill_name}", skill_name):
            for path in list(preset_dir.glob(pattern)):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                    found = True
                    prune_empty_dirs(path.parent, preset_dir)
                elif path.is_symlink() or path.is_file():
                    path.unlink()
                    found = True
                    prune_empty_dirs(path.parent, preset_dir)
        if found:
            removed.append(skill_name)
    prune_lockfile(preset_dir, names_set)
    return sorted(set(removed))


def _dual_list_toggle_preset(name: str, preset_dir: Path, config: dict) -> None:
    """Interactive toggle of preset skills alongside all available skills."""
    current_skills = get_preset_skills(preset_dir)

    candidates = []
    # Add skills currently in the preset first
    candidates.extend(rows_for(name, current_skills, scope="preset"))
    for row in candidates:
        row["location"] = f"in:{name}"

    # Add other installed skills from other presets and global folders
    seen = {row["skill"] for row in candidates}
    for other in sorted(d for d in PRESETS_DIR.iterdir() if d.is_dir() and d != preset_dir):
        for row in rows_for(other.name, get_preset_skills(other)):
            if row["skill"] not in seen:
                row["location"] = f"from:{other.name}"
                candidates.append(row)
                seen.add(row["skill"])

    for row in get_global_skills(config):
        if row["skill"] not in seen:
            candidates.append(row)
            seen.add(row["skill"])

    if not candidates:
        print_warn("No skills found in presets or global folders.")
        return

    header = (
        f"Editing preset '{name}' · {len(current_skills)} current skills\n"
        "Tab select skills to KEEP or ADD · Deselected in-preset skills are REMOVED · Enter apply"
    )

    if wants_picker(config):
        chosen = pick(candidates, header=header)
    else:
        labels = [f"{row.get('location', row['preset'])}/{row['skill']}" for row in candidates]
        chosen_labels = set(prompt_skills(labels, action="save"))
        chosen = [row for row, label in zip(candidates, labels) if label in chosen_labels]

    if not chosen:
        print_warn("Nothing selected; no changes made.")
        return

    chosen_skills = {row["skill"] for row in chosen}
    to_add = [row for row in chosen if row["skill"] not in current_skills]
    to_remove = [skill for skill in current_skills if skill not in chosen_skills]

    if not to_add and not to_remove:
        print_warn("No changes to apply.")
        return

    console.print()
    print_header(f"Preset '{name}' changes:")
    if to_add:
        console.print(f"  [choice]+ Add ({len(to_add)}):[/choice] {', '.join(r['skill'] for r in to_add)}")
    if to_remove:
        console.print(f"  [error]- Remove ({len(to_remove)}):[/error] {', '.join(to_remove)}")

    try:
        if not Confirm.ask("Apply these changes to preset?", default=True, console=console):
            return
    except (EOFError, KeyboardInterrupt):
        console.print()
        return

    if to_remove:
        removed_list = remove_skills_from_preset(preset_dir, to_remove)
        print_success(f"Removed {len(removed_list)} skill{'s' if len(removed_list) != 1 else ''} from '{name}': {', '.join(removed_list)}")

    if to_add:
        install_skills_into_preset(name, preset_dir, to_add)

    maybe_auto_push(f"edit preset {name}: sync skills")


def edit_preset(
    name: str = "",
    add_skills: Optional[list[str]] = None,
    remove_skills: Optional[list[str]] = None,
    config: Optional[dict] = None,
) -> None:
    """Interactively add, remove, and toggle skills in a preset."""
    config = config or load_config()
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)

    if not name:
        if sys.stdin.isatty():
            available = sorted(d.name for d in PRESETS_DIR.iterdir() if d.is_dir())
            if not available:
                print_warn("No presets found in ~/.skill-ctl/presets/")
                console.print("Create one with: [header]skctl presets create <name>[/header]")
                return
            name = prompt_preset(available, include_all=False)
        else:
            print_error("Error: Preset name is required. Usage: skctl presets edit <name>")
            sys.exit(1)

    preset_dir = preset_path(name)
    if not preset_dir.exists() or not preset_dir.is_dir():
        print_error(f"Preset '{name}' does not exist.")
        sys.exit(1)

    # CLI flags mode: non-interactive or batch
    if add_skills or remove_skills:
        if remove_skills:
            flat_rm = [s.strip() for item in remove_skills for s in item.split(",") if s.strip()]
            removed = remove_skills_from_preset(preset_dir, flat_rm)
            if removed:
                print_success(f"Removed {len(removed)} skill{'s' if len(removed) != 1 else ''} from '{name}': {', '.join(removed)}")
            else:
                print_warn(f"None of the specified skills were found in preset '{name}'.")

        if add_skills:
            flat_add = [s.strip() for item in add_skills for s in item.split(",") if s.strip()]
            candidates = []
            for other in sorted(d for d in PRESETS_DIR.iterdir() if d.is_dir() and d != preset_dir):
                candidates.extend(rows_for(other.name, get_preset_skills(other)))
            candidates.extend(get_global_skills(config))
            rows_to_add = [row for row in candidates if row["skill"] in flat_add]
            if rows_to_add:
                install_skills_into_preset(name, preset_dir, rows_to_add)
            else:
                print_warn(f"Could not find skills to add in local presets or global folders: {', '.join(flat_add)}")

        maybe_auto_push(f"edit preset {name}")
        return

    if not sys.stdin.isatty():
        print_error("Interactive preset editing requires a terminal. Or use --add / --remove.")
        sys.exit(1)

    while True:
        skills = get_preset_skills(preset_dir)
        skill_names = sorted(skills.keys())
        console.print()
        print_header(f"Editing preset '{name}'")
        console.print(f"[dim]Location: {preset_dir}[/dim]")
        if skill_names:
            preview_names = ", ".join(skill_names[:8])
            if len(skill_names) > 8:
                preview_names += f" [dim]+{len(skill_names) - 8} more[/dim]"
            console.print(f"[dim]Current skills ({len(skill_names)}):[/dim] {preview_names}")
        else:
            console.print("[dim]Current skills: (none)[/dim]")
        console.print()
        console.print("  [choice]1)[/choice] Add skills from installed presets / global")
        console.print("  [choice]2)[/choice] Search & add from skills.sh (remote)")
        console.print("  [choice]3)[/choice] Remove skills from this preset")
        console.print("  [choice]4)[/choice] Sync / toggle all (dual-list picker)")
        console.print("  [choice]5)[/choice] Done")

        try:
            choice = Prompt.ask("Choose an option", choices=["1", "2", "3", "4", "5"], default="5", console=console)
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if choice == "1":
            if add_installed_to_preset(name, preset_dir, config, warn_empty=True):
                maybe_auto_push(f"edit preset {name}: add skills")
        elif choice == "2":
            from skill_ctl.search import search
            search(preset=name)
            maybe_auto_push(f"edit preset {name}: search add skills")
        elif choice == "3":
            if not skill_names:
                print_warn(f"Preset '{name}' has no skills to remove.")
                continue
            rows = rows_for(name, skills)
            if wants_picker(config):
                chosen = pick(
                    rows,
                    header=f"Remove from '{name}' · {DEFAULT_HEADER.replace('applies', 'selects')}",
                    action="remove",
                )
                to_remove = [row["skill"] for row in chosen]
            else:
                to_remove = prompt_skills(skill_names, action="remove")
            if not to_remove:
                print_warn("Nothing selected to remove.")
                continue
            try:
                if not Confirm.ask(
                    f"Remove {len(to_remove)} skill{'s' if len(to_remove) != 1 else ''} from '{name}' ({', '.join(to_remove)})?",
                    default=False, console=console,
                ):
                    continue
            except (EOFError, KeyboardInterrupt):
                console.print()
                continue
            removed_list = remove_skills_from_preset(preset_dir, to_remove)
            print_success(f"Removed {len(removed_list)} skill{'s' if len(removed_list) != 1 else ''} from '{name}': {', '.join(removed_list)}")
            maybe_auto_push(f"edit preset {name}: remove skills")
        elif choice == "4":
            _dual_list_toggle_preset(name, preset_dir, config)
        elif choice == "5":
            break


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

    return install_skills_into_preset(name, preset_dir, chosen)


def add_installed_to_global(
    config: dict,
    agent: Optional[str] = None,
    use_copy: bool = False,
    force: bool = False,
    skill: Optional[str] = None,
    warn_empty: bool = True,
    yes: bool = False,
) -> int:
    """Pick skills from presets and link/copy them into global agent directories."""
    rows = []
    for directory in sorted(d for d in PRESETS_DIR.iterdir() if d.is_dir()) if PRESETS_DIR.is_dir() else []:
        rows.extend(rows_for(directory.name, get_preset_skills(directory)))
    if not rows:
        if warn_empty:
            print_warn("No skills in any preset yet.")
            console.print("Add one with: [header]skctl add <package> --preset <name>[/header]")
        return 0

    if skill:
        wanted = {s.strip() for s in skill.split(",") if s.strip()}
        matched_rows = [row for row in rows if row["skill"] in wanted]
        if not matched_rows:
            print_warn(f"Skill '{skill}' not found in any preset.")
            return 0
        if len(matched_rows) == len(wanted):
            chosen = matched_rows
        elif sys.stdin.isatty() and wants_picker(config):
            header = (
                f"\x1b[1;36mAll presets\x1b[0m\n"
                f"\x1b[2mAdding to\x1b[0m  \x1b[2;32mGlobal\x1b[0m\n"
                f"{DEFAULT_HEADER}"
            )
            chosen = pick(matched_rows, header=header)
        elif sys.stdin.isatty():
            chosen = pick_from_labels(matched_rows)
        else:
            seen = set()
            chosen = []
            for row in matched_rows:
                if row["skill"] not in seen:
                    seen.add(row["skill"])
                    chosen.append(row)
    else:
        if not sys.stdin.isatty():
            print_error("Interactive skill selection requires a terminal.")
            return 0
        print_header(f"{len(rows)} skills across {len({row['preset'] for row in rows})} presets")
        header = (
            f"\x1b[1;36mAll presets\x1b[0m\n"
            f"\x1b[2mAdding to\x1b[0m  \x1b[2;32mGlobal\x1b[0m\n"
            f"{DEFAULT_HEADER}"
        )
        chosen = pick(rows, header=header) if wants_picker(config) else pick_from_labels(rows)

    if not chosen:
        print_warn("Nothing picked.")
        return 0

    seen: dict[str, dict] = {}
    duplicates = []
    for row in chosen:
        name = row["skill"]
        if name in seen:
            duplicates.append((name, seen[name]["preset"], row["preset"]))
        seen[name] = row
    if duplicates:
        for name, p1, p2 in duplicates:
            print_warn(f"Multiple versions of '{name}' chosen ({p1}, {p2}); using '{p2}'.")

    skills = {name: Path(row["path"]) for name, row in seen.items()}

    agent_map = get_agent_dir_map(config)
    if agent in ("*", "all"):
        dest_rel_dirs = list(dict.fromkeys(agent_map.values()))
    elif agent:
        dest_rel_dirs = [agent_map.get(agent, f".{agent}/skills")]
    elif config.get("default_agents"):
        dest_rel_dirs = list(dict.fromkeys(agent_map.get(a, f".{a}/skills") for a in config["default_agents"]))
    else:
        from skill_ctl.runner import get_detected_global_agents
        dest_rel_dirs = list(dict.fromkeys(agent_map.get(a, f".{a}/skills") for a in get_detected_global_agents()))

    if not yes and sys.stdin.isatty():
        skill_count = len(skills)
        names_str = ", ".join(sorted(skills.keys()))
        dest_labels = [f"~/{d}" for d in dest_rel_dirs]
        prompt_msg = f"Apply {skill_count} skill{'s' if skill_count != 1 else ''} ({names_str}) globally to {', '.join(dest_labels)}?"
        try:
            confirmed = Confirm.ask(prompt_msg, default=True, console=console)
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if not confirmed:
            print_warn("Cancelled.")
            return 0

    target_project = Path.home()
    copy_mode = use_copy or (config.get("apply_mode") == "copy")
    result = apply_skills(target_project, skills, dest_rel_dirs, copy_mode, force=force)
    for path in result.kept:
        print_warn(f"Kept existing ~/{path} (not a link; --force replaces it)")
    verb = "Copied" if result.copied else "Linked"
    dest_labels = [f"~/{d}" for d in dest_rel_dirs]
    if result.applied:
        print_success(f"{verb} {len(result.applied)} skill{'s' if len(result.applied) != 1 else ''} into {', '.join(dest_labels)}")
    return len(result.applied)


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
        from skill_ctl.search import search

        search(preset=name)


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
    if project == Path.home():
        return found
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
    is_home = target_project == Path.home()
    target_label = "globally" if is_home else f"to {target_project}"
    print_header(f"Applying preset '{target_preset}' ({len(skills)} skills) {target_label}")
    result = apply_skills(target_project, skills, dest_rel_dirs, use_copy, force, owned)
    for path in result.kept:
        prefix = "~/" if is_home else ""
        print_warn(f"Kept existing {prefix}{path} (not a link; --force replaces it)")
    verb = "Copied" if result.copied else "Linked"
    dest_labels = [f"~/{d}" for d in dest_rel_dirs] if is_home else dest_rel_dirs
    print_success(f"{verb} {len(result.applied)} skill{'s' if len(result.applied) != 1 else ''} into {', '.join(dest_labels)}")
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
    global_unapply: Annotated[
        bool,
        typer.Option("--global", "-g", help="Remove preset skills from global user agent directories.")
    ] = False,
    skill: Annotated[
        Optional[str],
        typer.Option("--skill", "-s", help="Only unapply these skills (comma-separated).")
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
    """Remove a preset's skills from the project or global agent directories.

    With no preset named, it removes what `apply` recorded for this project, or
    the default preset if there is no record. It deletes only what the preset
    put there, then removes the agent directories it emptied and its entries in
    skills-lock.json.
    """
    config = load_config()
    interactive = sys.stdin.isatty() and config.get("prompts", {}).get("ask_apply", True)

    if not project and not global_unapply and interactive:
        dest_choice = prompt_apply_destination(Path.cwd())
        if dest_choice == "2":
            global_unapply = True

    if global_unapply:
        target_project = Path.home()
    else:
        target_project = Path(project).expanduser().resolve() if project else Path.cwd()

    if preset:
        chosen_presets = [preset]
    else:
        recorded = presets_for(target_project)
        chosen_presets = sorted(recorded)
        if not chosen_presets:
            # Check if any preset has links into target_project
            detected = []
            if PRESETS_DIR.is_dir():
                target_dirs = project_skill_dirs(target_project, config)
                for p in sorted(PRESETS_DIR.iterdir()):
                    if not p.is_dir():
                        continue
                    for rel_dir in target_dirs:
                        dir_path = target_project / rel_dir
                        if dir_path.is_dir():
                            for s in dir_path.iterdir():
                                if links_into_preset(s, p):
                                    detected.append(p.name)
                                    break
            chosen_presets = sorted(set(detected))

        if not chosen_presets:
            loc_label = "Global (~/)" if target_project == Path.home() else str(target_project)
            print_warn(f"Nothing is currently applied to {loc_label}.")
            return

    skill_list = [s.strip() for s in skill.split(",") if s.strip()] if skill else None

    if not yes and sys.stdin.isatty():
        label = ", ".join(chosen_presets)
        loc_label = "Global (~/)" if target_project == Path.home() else str(target_project)
        if skill_list:
            prompt_msg = f"Remove skill{'s' if len(skill_list) != 1 else ''} ({', '.join(skill_list)}) in preset{'s' if len(chosen_presets) != 1 else ''} '{label}' from {loc_label}?"
        else:
            prompt_msg = f"Remove preset{'s' if len(chosen_presets) != 1 else ''} '{label}' from {loc_label}?"
        try:
            confirmed = Confirm.ask(
                prompt_msg,
                default=False, console=console,
            )
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not confirmed:
            return

    for name in chosen_presets:
        unapply_one(name, target_project, agent, force, config, named=bool(preset), skill_filter=skill_list)


def unapply_one(
    target_preset: str,
    target_project: Path,
    agent: Optional[str],
    force: bool,
    config: dict,
    named: bool = True,
    skill_filter: Optional[Iterable[str]] = None,
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
    if skill_filter:
        fset = set(skill_filter)
        names = [n for n in names if n in fset]
    if not names:
        if skill_filter:
            print_warn(f"None of the specified skills found in preset '{target_preset}'.")
        else:
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

    is_home = target_project == Path.home()
    target_label = "globally (~/)" if is_home else f"from project at {target_project}"
    print_header(f"Removing preset '{target_preset}' skills {target_label}")
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

        preview_command = preview_markdown_command("{5}")
        result = subprocess.run(
            [
                "fzf", "--ansi", "--multi", "--expect", "alt-x,alt-e,alt-r,alt-n,alt-y,alt-m,alt-t", "--tabstop", "2",
                "--delimiter", "\t", "--with-nth", "2,3,4",
                "--header", "Preset                  Skills  Applied projects\nTab select · Enter info · Alt-T edit · Alt-N new · Alt-Y clone · Alt-M combine · Alt-R rename · Alt-X delete · Alt-E export",
                "--color", fzf_color_arg(config.get("theme")),
                "--preview", preview_command, "--preview-window", "right,55%,wrap",
            ],
            input="\n".join(rows), stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None, []
        selected = result.stdout.splitlines()
        action = selected.pop(0) if selected[0] in ("alt-x", "alt-e", "alt-r", "alt-n", "alt-y", "alt-m", "alt-t") else None
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
    action: Annotated[Literal["browse", "list", "applied", "create", "edit", "clone", "combine", "history", "rename", "delete", "export", "import"], typer.Argument()] = "browse",
    name: Annotated[str, typer.Argument()] = "",
    new_name: Annotated[str, typer.Argument()] = "",
    source_names: Annotated[list[str], typer.Argument()] = [],
    from_path: Annotated[Optional[str], typer.Option("--from", help="Copy skills from this path when creating a preset.")] = None,
    clean: Annotated[bool, typer.Option("--clean")] = False,
    output: Annotated[Optional[str], typer.Option("--output", "-o", help="Output path for `presets export`.")] = None,
    import_presets: Annotated[Optional[list[str]], typer.Option("--preset", "-P", help="Preset to import from a GitHub repository; repeat to import several.")] = None,
    import_rename: Annotated[Optional[str], typer.Option("--rename", help="New name for an imported preset.")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Replace an existing preset when importing, cloning, or combining.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview an import, clone, or combine without writing files.")] = False,
    add_skills: Annotated[Optional[list[str]], typer.Option("--add", "-a", help="Skill(s) to add to the preset.")] = None,
    remove_skills: Annotated[Optional[list[str]], typer.Option("--remove", "-r", help="Skill(s) to remove from the preset.")] = None,
) -> None:
    """Create, clone, combine, rename, delete, export, and import presets.

    Examples: `skctl presets edit DEV`,
    `skctl presets clone BASE NEW`,
    `skctl presets combine DEV FRONTEND BACKEND`,
    `skctl presets history DEV`,
    `skctl presets export NAME -o ARCHIVE`,
    `skctl presets import ARCHIVE --rename NAME`, or
    `skctl presets import OWNER/REPO --preset NAME`.

    Deleting the default_preset leaves an empty one behind, because `apply` and
    the add prompt both fall back to it.
    """
    from rich import box
    from rich.table import Table

    if from_path and action != "create":
        print_error("--from is only available with `skctl presets create`.")
        sys.exit(1)
    if action == "edit":
        edit_preset(name, add_skills=add_skills, remove_skills=remove_skills, config=load_config())
        return
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
            print_error("Error: Usage: skctl presets import <archive|owner/repo> [--preset <name>] [--rename <name>] [--replace]")
            sys.exit(1)
        if Path(name).expanduser().is_file():
            if import_presets:
                print_error("--preset is only available when importing a GitHub repository.")
                sys.exit(1)
            preset_import(name, import_rename, replace, dry_run)
        else:
            import_github_presets(name, import_presets or [], import_rename, replace, dry_run)
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
            if picker_action == "alt-t":
                if len(picked) != 1:
                    print_warn("Select one preset to edit.")
                else:
                    edit_preset(picked[0], config=config)
            elif picker_action == "alt-x":
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
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Inspect status for a specific project directory (default: current directory).")
    ] = None,
    global_status: Annotated[
        bool,
        typer.Option("--global", "-g", help="Inspect global skills status (~/.agents/skills, ~/.claude/skills, ...).")
    ] = False,
    all_projects: Annotated[
        bool,
        typer.Option("--all", "-a", help="Show status across all recorded projects.")
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed breakdown of all individual skills.")
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output machine-readable JSON.")
    ] = False,
) -> None:
    """Show comprehensive skill, preset, and directory status."""
    import json as jsonlib
    from rich import box
    from rich.table import Table

    config = load_config()

    if all_projects:
        applied_map = load_applied()
        if json_output:
            print(jsonlib.dumps(applied_map, indent=2))
            return

        print_header("Skill status across all projects")
        if not applied_map:
            console.print("  [dim]No projects have applied presets recorded yet.[/dim]")
            return

        table = Table(box=box.ROUNDED, header_style="header")
        table.add_column("Project / Scope", style="choice")
        table.add_column("Presets", style="header")
        table.add_column("Total Skills", justify="right")
        table.add_column("Mode")
        table.add_column("Targets")

        for proj_path_str, presets_dict in sorted(applied_map.items()):
            preset_names = ", ".join(sorted(presets_dict.keys()))
            all_skills = sorted({s for entry in presets_dict.values() for s in entry.get("skills", [])})
            modes = ", ".join(sorted({entry.get("mode", "symlink") for entry in presets_dict.values()}))
            all_targets = ", ".join(sorted({t for entry in presets_dict.values() for t in entry.get("targets", [])}))

            p_obj = Path(proj_path_str)
            try:
                p_label = "~/" + p_obj.relative_to(Path.home()).as_posix() if p_obj.is_relative_to(Path.home()) else proj_path_str
            except Exception:
                p_label = proj_path_str
            if p_obj == Path.home():
                p_label = "Global (~/)"

            table.add_row(p_label, preset_names, str(len(all_skills)), modes, all_targets or "[dim]auto[/dim]")
        console.print(table)
        return

    if global_status:
        target = Path.home()
        is_global = True
    elif project:
        target = Path(project).expanduser().resolve()
        is_global = (target == Path.home())
    else:
        target = Path.cwd()
        is_global = (target == Path.home())

    recorded = presets_for(target)
    dirs = project_skill_dirs(target, config)

    # Scan directory skills on disk
    disk_folders = {}
    for rel_dir in dirs:
        dir_path = target / rel_dir
        if dir_path.is_dir():
            skills_in_dir = {}
            for item in sorted(dir_path.iterdir()):
                if not (item.is_dir() or item.is_symlink()):
                    continue
                origin = "standalone"
                if item.is_symlink():
                    try:
                        resolved = item.resolve()
                        if PRESETS_DIR.resolve() in resolved.parents:
                            preset_name = resolved.relative_to(PRESETS_DIR.resolve()).parts[0]
                            origin = f"link → {preset_name}"
                        else:
                            origin = "symlink"
                    except Exception:
                        origin = "broken link"
                else:
                    for p_name, p_entry in recorded.items():
                        if item.name in p_entry.get("skills", []) and p_entry.get("mode") == "copy":
                            origin = f"copy → {p_name}"
                            break
                skills_in_dir[item.name] = {
                    "origin": origin,
                    "is_symlink": item.is_symlink(),
                    "valid": item.exists(),
                    "path": str(item),
                }
            if skills_in_dir:
                disk_folders[rel_dir] = skills_in_dir

    if json_output:
        data = {
            "target": str(target),
            "is_global": is_global,
            "recorded_presets": recorded,
            "disk_folders": disk_folders,
        }
        print(jsonlib.dumps(data, indent=2))
        return

    try:
        target_display = "~/" + target.relative_to(Path.home()).as_posix() if target.is_relative_to(Path.home()) else str(target)
    except Exception:
        target_display = str(target)
    if is_global:
        target_display = "Global (~/)"

    print_header(f"Skill status for {target_display}")

    # Section 1: Applied Presets
    if recorded:
        table = Table(box=box.ROUNDED, header_style="header")
        table.add_column("Preset", style="choice")
        table.add_column("Skills", style="header")
        table.add_column("Mode")
        table.add_column("Targets")
        table.add_column("Status")

        for name, entry in sorted(recorded.items()):
            skill_names = entry.get("skills", [])
            preset_dir = preset_path(name)
            targets_list = entry.get("targets") or dirs

            if not preset_dir.exists():
                state = "[warn]! preset missing[/warn]"
            else:
                missing = sum(
                    not any((target / rel_dir / skill).exists() for rel_dir in targets_list)
                    for skill in skill_names
                )
                if missing == 0:
                    state = "[success]✓ active[/success]"
                else:
                    state = f"[error]✗ {missing} missing[/error]"

            if len(skill_names) <= 3:
                skills_label = f"{len(skill_names)} ({', '.join(skill_names)})" if skill_names else "[dim]0[/dim]"
            else:
                skills_label = f"{len(skill_names)} ({', '.join(skill_names[:3])}, +{len(skill_names) - 3} more)"

            targets_str = ", ".join(targets_list) if targets_list else "[dim]default[/dim]"
            table.add_row(name, skills_label, str(entry.get("mode", "symlink")), targets_str, state)
        console.print(table)
    else:
        console.print("  [dim]No presets recorded for this project.[/dim]")

    # Section 2: Active Skill Directories on Disk
    if disk_folders:
        console.print()
        console.print("[bold]Active Agent Directories on Disk:[/bold]")
        for rel_dir, s_map in disk_folders.items():
            dir_label = f"~/{rel_dir}" if is_global else f"./{rel_dir}"
            console.print(f"  [choice]📁 {dir_label}[/choice] [dim]({len(s_map)} skill{'s' if len(s_map) != 1 else ''})[/dim]")

            items = list(s_map.items())
            show_items = items if (verbose or len(items) <= 8) else items[:6]
            for s_name, s_info in show_items:
                orig = s_info["origin"]
                if "broken" in orig:
                    marker = "[error]✗[/error]"
                elif "link" in orig:
                    marker = "[success]•[/success]"
                else:
                    marker = "[choice]•[/choice]"
                console.print(f"     {marker} {s_name} [dim]({orig})[/dim]")
            if len(items) > len(show_items):
                console.print(f"     [dim]... and {len(items) - len(show_items)} more (use --verbose / -v to see all)[/dim]")
    elif not recorded:
        console.print("  [dim]No skill folders found on disk.[/dim]")

    # Section 3: Global Context (when viewing local project)
    if not is_global:
        global_rec = presets_for(Path.home())
        global_skill_count = 0
        for g_dir in (Path.home() / ".agents" / "skills", Path.home() / ".claude" / "skills"):
            if g_dir.is_dir():
                global_skill_count += len([x for x in g_dir.iterdir() if x.is_dir() or x.is_symlink()])
        console.print()
        if global_rec:
            g_presets_str = ", ".join(f"[choice]{k}[/choice] ({len(v.get('skills', []))} skills)" for k, v in global_rec.items())
            console.print(f"[dim]Global Scope (~/): {g_presets_str} · {global_skill_count} installed · [italic]skctl status -g[/italic][/dim]")
        elif global_skill_count > 0:
            console.print(f"[dim]Global Scope (~/): {global_skill_count} skills installed · [italic]skctl status -g[/italic][/dim]")

    # Section 4: Presets Library Glance
    if PRESETS_DIR.is_dir():
        preset_count = len([p for p in PRESETS_DIR.iterdir() if p.is_dir()])
        def_preset = config.get("default_preset", "default")
        console.print(f"[dim]Presets Library: {preset_count} presets in ~/.skill-ctl/presets (default: '{def_preset}') · [italic]skctl presets[/italic][/dim]")
