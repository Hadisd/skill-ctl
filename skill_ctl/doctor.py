"""Finding and clearing skill links that no longer point anywhere (doctor)."""

from pathlib import Path
from typing import Annotated, Optional

import typer

from skill_ctl.config import load_config, all_target_dirs
from skill_ctl.ui import console, print_header, print_success, print_warn
from skill_ctl.linker import prune_empty_dirs
from skill_ctl.presets import preset_path
from skill_ctl.registry import load_applied, save_applied, presets_for, write_project_file


def broken_links(project: Path, rel_dirs: list) -> list[Path]:
    """Symlinks under the project's agent directories whose target is gone.

    `exists()` follows the link, so a link into a deleted preset reads as missing
    while the link itself is still there.
    """
    found = []
    for rel_dir in rel_dirs:
        directory = project / rel_dir
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink() and not entry.exists():
                found.append(entry)
    return found


def doctor(
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Project to check (default: current directory).")
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="Check every project that `apply` has recorded.")
    ] = False,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Delete the broken links and drop stale records.")
    ] = False,
) -> None:
    """Check applied projects for skill links that lead nowhere.

    Deleting a preset leaves dangling symlinks in every project that used it.
    This finds them. --fix deletes them and the directories they empty.
    """
    rel_dirs = all_target_dirs(load_config())
    applied = load_applied()

    if all_projects:
        projects = [Path(p) for p in sorted(applied)]
        if not projects:
            print_warn("No projects recorded yet; `apply` records them from now on.")
            return
    else:
        projects = [Path(project).expanduser().resolve() if project else Path.cwd()]

    total_broken = 0
    gone = []

    for target in projects:
        if not target.is_dir():
            gone.append(str(target))
            print_warn(f"{target} no longer exists (recorded as applied)")
            continue

        # Read through the project's own record, so a clone that carries one is
        # checked even though this machine's index has never seen it.
        recorded = presets_for(target)
        dangling = broken_links(target, rel_dirs)
        missing_presets = [name for name in recorded if not preset_path(name).exists()]
        if not dangling and not missing_presets:
            if not all_projects:
                print_success(f"{target} looks fine.")
            continue

        print_header(str(target))
        # A deleted preset leaves dangling links, which --fix can just delete. Its
        # copies are real directories, which only `unapply --force` should remove,
        # so the record of them has to stay until it does.
        stranded_copies = [
            name for name in missing_presets
            if (recorded.get(name) or {}).get("mode") == "copy"
        ]
        for name in missing_presets:
            if name in stranded_copies:
                print_warn(f"Preset '{name}' is gone, but its copied skills are still here.")
                console.print(f"    Remove them with: [header]skctl unapply {name} --force[/header]")
            else:
                print_warn(f"Preset '{name}' is recorded here but no longer exists.")
        for link in dangling:
            total_broken += 1
            console.print(f"    broken: {link.relative_to(target)} -> {link.readlink()}")
            if fix:
                link.unlink()

        if fix:
            for link in dangling:
                pruned = prune_empty_dirs(link.parent, target)
                if pruned:
                    print_success(f"Removed empty {pruned.relative_to(target)}/")
            for name in missing_presets:
                if name not in stranded_copies:
                    recorded.pop(name, None)
            write_project_file(target, recorded)
            if recorded:
                applied[str(target)] = recorded
            else:
                applied.pop(str(target), None)

    if fix:
        for path in gone:
            applied.pop(path, None)
        applied = {path: presets for path, presets in applied.items() if presets}
        save_applied(applied)

    if total_broken:
        if fix:
            print_success(f"Removed {total_broken} broken link(s).")
        else:
            print_warn(f"{total_broken} broken link(s). Remove them with: skctl doctor --fix")
    elif all_projects:
        print_success(f"Checked {len(projects)} project(s); nothing broken.")
