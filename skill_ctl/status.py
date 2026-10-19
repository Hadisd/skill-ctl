"""Status command showing active skills, applied presets, and disk linkage."""

import json as jsonlib
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich import box
from rich.table import Table

from skill_ctl.config import load_config, project_skill_dirs
from skill_ctl.constants import PRESETS_DIR
from skill_ctl.registry import load_applied, presets_for
from skill_ctl.ui import console, print_error, print_header


def _preset_path(preset_name: str) -> Path:
    name = str(preset_name)
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        print_error(f"Invalid preset name '{name}': must be a single directory name.")
        sys.exit(1)
    return PRESETS_DIR / name


def status(
    project: Annotated[
        Optional[str],
        typer.Option(
            "--project",
            "-p",
            help="Inspect status for a specific project directory (default: current directory).",
        ),
    ] = None,
    global_status: Annotated[
        bool,
        typer.Option(
            "--global",
            "-g",
            help="Inspect global skills status (~/.agents/skills, ~/.claude/skills, ...).",
        ),
    ] = False,
    all_projects: Annotated[
        bool, typer.Option("--all", "-a", help="Show status across all recorded projects.")
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed breakdown of all individual skills."),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output machine-readable JSON.")
    ] = False,
) -> None:
    """Show comprehensive skill, preset, and directory status."""
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
            all_skills = sorted(
                {s for entry in presets_dict.values() for s in entry.get("skills", [])}
            )
            modes = ", ".join(
                sorted({entry.get("mode", "symlink") for entry in presets_dict.values()})
            )
            all_targets = ", ".join(
                sorted({t for entry in presets_dict.values() for t in entry.get("targets", [])})
            )

            p_obj = Path(proj_path_str)
            try:
                p_label = (
                    "~/" + p_obj.relative_to(Path.home()).as_posix()
                    if p_obj.is_relative_to(Path.home())
                    else proj_path_str
                )
            except Exception:
                p_label = proj_path_str
            if p_obj == Path.home():
                p_label = "Global (~/)"

            table.add_row(
                p_label, preset_names, str(len(all_skills)), modes, all_targets or "[dim]auto[/dim]"
            )
        console.print(table)
        return

    if global_status:
        target = Path.home()
        is_global = True
    elif project:
        target = Path(project).expanduser().resolve()
        is_global = target == Path.home()
    else:
        target = Path.cwd()
        is_global = target == Path.home()

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
        target_display = (
            "~/" + target.relative_to(Path.home()).as_posix()
            if target.is_relative_to(Path.home())
            else str(target)
        )
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
            preset_dir = _preset_path(name)
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
                skills_label = (
                    f"{len(skill_names)} ({', '.join(skill_names)})"
                    if skill_names
                    else "[dim]0[/dim]"
                )
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
            console.print(
                f"  [choice]📁 {dir_label}[/choice] [dim]({len(s_map)} skill{'s' if len(s_map) != 1 else ''})[/dim]"
            )

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
                console.print(
                    f"     [dim]... and {len(items) - len(show_items)} more (use --verbose / -v to see all)[/dim]"
                )
    elif not recorded:
        console.print("  [dim]No skill folders found on disk.[/dim]")

    # Section 3: Global Context (when viewing local project)
    if not is_global:
        global_rec = presets_for(Path.home())
        global_skill_count = 0
        for g_dir in (Path.home() / ".agents" / "skills", Path.home() / ".claude" / "skills"):
            if g_dir.is_dir():
                global_skill_count += len(
                    [x for x in g_dir.iterdir() if x.is_dir() or x.is_symlink()]
                )
        console.print()
        if global_rec:
            g_presets_str = ", ".join(
                f"[choice]{k}[/choice] ({len(v.get('skills', []))} skills)"
                for k, v in global_rec.items()
            )
            console.print(
                f"[dim]Global Scope (~/): {g_presets_str} · {global_skill_count} installed · [italic]skctl status -g[/italic][/dim]"
            )
        elif global_skill_count > 0:
            console.print(
                f"[dim]Global Scope (~/): {global_skill_count} skills installed · [italic]skctl status -g[/italic][/dim]"
            )

    # Section 4: Presets Library Glance
    if PRESETS_DIR.is_dir():
        preset_count = len([p for p in PRESETS_DIR.iterdir() if p.is_dir()])
        def_preset = config.get("default_preset", "default")
        console.print(
            f"[dim]Presets Library: {preset_count} presets in ~/.skill-ctl/presets (default: '{def_preset}') · [italic]skctl presets[/italic][/dim]"
        )
