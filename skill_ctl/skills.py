"""Direct skill commands wrapping npx skills (add, list, remove, update)."""

import re
import shutil
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.prompt import Confirm

from skill_ctl.backup import maybe_auto_push
from skill_ctl.config import load_config
from skill_ctl.constants import PRESETS_DIR
from skill_ctl.picker import DEFAULT_HEADER, pick, rows_for, wants_picker
from skill_ctl.presets import (
    ensure_preset_dir,
    get_preset_skills,
    preset_path,
)
from skill_ctl.prompts import prompt_destination, prompt_skills
from skill_ctl.runner import get_detected_global_agents, run_npx_skills
from skill_ctl.ui import console, print_error, print_header, print_success, print_warn


def add(
    package: Annotated[
        Optional[str],
        typer.Argument(
            help="Package name or GitHub repo from skills.sh (e.g. owner/repo). Omit to search skills.sh interactively."
        ),
    ] = None,
    preset: Annotated[
        Optional[str],
        typer.Option(
            "--preset", "-P", help="Preset to install into (~/.skill-ctl/presets/<name>/)."
        ),
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option(
            "--project",
            "-p",
            help="Project path to install directly into (default: current directory).",
        ),
    ] = None,
    global_install: Annotated[
        bool, typer.Option("--global", "-g", help="Install globally instead of project-level.")
    ] = False,
    skill: Annotated[
        Optional[str], typer.Option("--skill", "-s", help="Specific skill name to install.")
    ] = None,
    agent: Annotated[
        Optional[str],
        typer.Option(
            "--agent",
            "-a",
            help="Target agent (e.g. universal, claude-code, cursor, or '*' for all).",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts.")] = False,
    copy: Annotated[
        bool, typer.Option("--copy", "-c", help="Copy files instead of symlinking.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Force overwrite existing skill directories.")
    ] = False,
    all_skills: Annotated[
        bool, typer.Option("--all", help="Shorthand for --skill '*' --agent '*' -y.")
    ] = False,
    full_depth: Annotated[
        bool, typer.Option("--full-depth", help="Search all subdirectories for SKILL.md.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output machine-readable JSON.")
    ] = False,
) -> None:
    """Add a skill package from skills.sh (or Git repo). Can target a preset, project, or global."""
    if package is None and not sys.stdin.isatty():
        print_error("Adding a skill requires a terminal or a package name.")
        sys.exit(1)

    config = load_config()
    default_dest = config.get("default_destination", "prompt")
    ask_dest = config.get("prompts", {}).get("ask_destination", True)
    default_preset_name = config.get("default_preset", "default")

    if package is None and sys.stdin.isatty():
        console.print("[dim]No skill package specified. Find and install from skills.sh:[/dim]")

    # If no destination flag was provided, determine target from config or prompt
    if preset is None and not global_install and project is None:
        if default_dest == "global":
            global_install = True
        elif default_dest == "project":
            project = "."
        elif default_dest == "preset":
            preset = default_preset_name
        elif ask_dest and sys.stdin.isatty() and not yes:
            preset, project_flag, global_install = prompt_destination(
                PRESETS_DIR, default_preset=default_preset_name, target_project=Path.cwd()
            )
            if project_flag:
                project = "."
            elif preset is None and not global_install:
                return
        else:
            preset = default_preset_name

    if package is None:
        from skill_ctl.search import search

        search(preset=preset, project=project, global_only=global_install, remote_only=True)
        return

    if agent and agent not in ("*", "all"):
        if (
            "/" in agent
            or "\\" in agent
            or ".." in agent
            or not re.match(r"^[a-zA-Z0-9_.-]+$", agent)
        ):
            print_error(f"Invalid agent name '{agent}': must be an alphanumeric identifier.")
            sys.exit(1)

    # Smart Headless Driver: Pre-resolve skills and agents when not explicitly provided
    resolved_skills = [skill] if skill else []
    resolved_agents = [agent] if agent else []

    if not skill and not all_skills and "@" not in package and sys.stdin.isatty() and not yes:
        from skill_ctl.catalog import choose_repo_skills, fetch_repo_skills

        try:
            repo_skills = fetch_repo_skills(package)
            if len(repo_skills) == 1:
                console.print(
                    f"[dim]Found 1 skill in {package}: [bold]{repo_skills[0]}[/bold][/dim]"
                )
                resolved_skills = [repo_skills[0]]
            elif len(repo_skills) > 1:
                picked = choose_repo_skills(package, repo_skills)
                if not picked:
                    console.print("[dim]No skills selected. Aborted.[/dim]")
                    return
                resolved_skills = picked
        except Exception:
            pass

    if not agent and not config.get("default_agents"):
        if preset:
            resolved_agents = ["universal"]
        elif global_install:
            resolved_agents = get_detected_global_agents()
        else:
            target_path = Path(project).expanduser().resolve() if project else Path.cwd()
            detected_project_agents = []
            if (target_path / ".agents").is_dir():
                detected_project_agents.append("universal")
            if (target_path / ".claude").is_dir():
                detected_project_agents.append("claude-code")
            if (target_path / ".cursor").is_dir():
                detected_project_agents.append("cursor")
            if (target_path / ".codex").is_dir():
                detected_project_agents.append("codex")
            if (target_path / ".windsurf").is_dir() or (
                target_path / ".codeium" / "windsurf"
            ).is_dir():
                detected_project_agents.append("windsurf")
            if (target_path / ".hermes").is_dir():
                detected_project_agents.append("hermes")
            if (target_path / ".github").is_dir():
                detected_project_agents.append("github-copilot")
            resolved_agents = detected_project_agents if detected_project_agents else ["universal"]
    elif not agent and config.get("default_agents"):
        resolved_agents = list(config["default_agents"])

    npx_args = [package]
    for s in resolved_skills:
        npx_args.extend(["--skill", s])
    for a in resolved_agents:
        npx_args.extend(["--agent", a])

    # If both skills and agents were pre-resolved, drive npx skills headlessly (-y)
    drive_headless = (bool(resolved_skills) or all_skills or "@" in package) and bool(
        resolved_agents
    )

    # Pass -y if requested or if driving headlessly
    if yes or drive_headless:
        npx_args.append("-y")

    if copy:
        npx_args.append("--copy")
    if all_skills:
        npx_args.append("--all")
    if full_depth:
        npx_args.append("--full-depth")
    if json_output:
        npx_args.append("--json")

    if preset:
        preset_dir = ensure_preset_dir(preset)
        print_header(f"Adding skill to preset '{preset}' (~/.skill-ctl/presets/{preset})")
        code = run_npx_skills(["add"] + npx_args, cwd=str(preset_dir))
        if code == 0:
            maybe_auto_push(f"add {package} to {preset}")
    else:
        if global_install:
            npx_args.append("-g")
            # If running non-interactively with -y and no agent was specified,
            # use detected agents to prevent npx skills from crashing on PromptScript.
            if yes and not agent and not config.get("default_agents"):
                for a in get_detected_global_agents():
                    npx_args.extend(["--agent", a])
            target_cwd = Path.cwd()
        else:
            target_path = Path(project).expanduser().resolve() if project else Path.cwd()
            target_path.mkdir(parents=True, exist_ok=True)
            target_cwd = target_path
            if str(target_cwd) != str(Path.cwd()):
                print_header(f"Adding skill to project at {target_cwd}")
        code = run_npx_skills(["add"] + npx_args, cwd=str(target_cwd))

    if code != 0:
        sys.exit(code)


from skill_ctl.scanner import (  # noqa: F401
    AGENT_NAME_EXPANSIONS,
    _display_path,
    _format_agents_summary,
    scan_installed_skills,
)


def list_skills(
    preset: Annotated[
        Optional[str], typer.Option("--preset", "-P", help="Preset to inspect skills for.")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option(
            "--project",
            "-p",
            help="Inspect skills for a specific project directory (default: current directory).",
        ),
    ] = None,
    global_list: Annotated[
        bool, typer.Option("--global", "-g", help="List global skills.")
    ] = False,
    agent: Annotated[
        Optional[str],
        typer.Option(
            "--agent", "-a", help="Filter by agent (e.g. universal, claude-code, cursor)."
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
    npx: Annotated[
        bool, typer.Option("--npx", help="Use npx skills' original list implementation.")
    ] = False,
) -> None:
    """List installed skills in current project, a preset, or global."""
    if preset:
        p = preset_path(preset)
        if not p.exists():
            print_error(f"Preset '{preset}' not found.")
            sys.exit(1)
        skills = get_preset_skills(p)
        print_header(f"Skills in preset '{preset}':")
        for s in sorted(skills.keys()):
            console.print(f"  [choice]- {s}[/choice]")
        return

    if npx:
        args = ["list"]
        if global_list:
            args.append("-g")
        if agent:
            args.extend(["--agent", agent])
        if json_output:
            args.append("--json")
        target_cwd = Path(project).expanduser().resolve() if project else Path.cwd()
        code = run_npx_skills(args, cwd=str(target_cwd))
        if code != 0:
            sys.exit(code)
        return

    import json
    import warnings

    if shutil.which("npx") is None:
        warnings.warn("'npx' not found on PATH. Install Node.js and retry.", RuntimeWarning)
        sys.exit(1)

    target_root = (
        Path.home()
        if global_list
        else (Path(project).expanduser().resolve() if project else Path.cwd())
    )
    is_global = global_list or (target_root == Path.home())

    skills_list = scan_installed_skills(target_root, is_global=is_global, agent_filter=agent)

    if json_output:
        print(json.dumps(skills_list, indent=2))
        return

    title = "Global Skills" if is_global else "Project Skills"
    console.print(f"[header]{title}[/header]\n")
    if not skills_list:
        console.print(
            f"  [dim]No skills installed {'globally' if is_global else 'in current project'}.[/dim]"
        )
        return

    for item in skills_list:
        display_path = _display_path(item["path"])
        agents_str = _format_agents_summary(item["agents"])
        src_str = item["source"] or "local"
        console.print(f"[choice]{item['name']:<24}[/choice] [path]{display_path}[/path]")
        console.print(f"  [dim]Agents:[/dim] {agents_str}  [dim]Source:[/dim] {src_str}")


def remove(
    skill: Annotated[Optional[str], typer.Argument()] = None,
    preset: Annotated[
        Optional[str], typer.Option("--preset", "-P", help="Remove skill from this preset.")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option(
            "--project",
            "-p",
            help="Remove from a specific project directory (default: current directory).",
        ),
    ] = None,
    global_rm: Annotated[
        bool, typer.Option("--global", "-g", help="Remove skill from global scope.")
    ] = False,
    agent: Annotated[
        Optional[str],
        typer.Option(
            "--agent",
            "-a",
            help="Remove from specific agent only (e.g. universal, claude-code, cursor).",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")] = False,
) -> None:
    """Remove an installed skill. Omit the name to pick interactively."""
    if agent and agent not in ("*", "all"):
        if (
            "/" in agent
            or "\\" in agent
            or ".." in agent
            or not re.match(r"^[a-zA-Z0-9_.-]+$", agent)
        ):
            print_error(f"Invalid agent name '{agent}': must be an alphanumeric identifier.")
            sys.exit(1)
    config = load_config()
    if preset and skill is None:
        p = preset_path(preset)
        if not p.exists():
            print_error(f"Preset '{preset}' not found.")
            sys.exit(1)
        if not sys.stdin.isatty():
            print_error("Interactive preset removal requires a terminal or a skill name.")
            sys.exit(1)
        skills = get_preset_skills(p)
        if not skills:
            print_warn(f"Preset '{preset}' has no installed skills.")
            return
        rows = rows_for(preset, skills)
        if wants_picker(config):
            chosen = pick(
                rows,
                header=f"Remove from {preset} · {DEFAULT_HEADER.replace('applies', 'selects')}",
                action="remove",
            )
            names = [row["skill"] for row in chosen]
        else:
            names = prompt_skills(sorted(skills), action="remove")
        if not names:
            print_warn("Nothing picked.")
            return
        if not yes:
            try:
                confirmed = Confirm.ask(
                    f"Remove {len(names)} skill{'s' if len(names) != 1 else ''} from '{preset}'?",
                    default=False,
                    console=console,
                )
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if not confirmed:
                return
        code = run_npx_skills(["remove", *names, "-y"], cwd=str(p))
        if code == 0:
            maybe_auto_push(f"remove {', '.join(names)} from {preset}")
        else:
            sys.exit(code)
        return

    # npx skills prompts with a multi-select picker when given no skill name.
    args = ["remove"] + ([skill] if skill else [])
    if agent:
        args.extend(["--agent", agent])
    if yes:
        args.append("-y")

    if preset:
        p = preset_path(preset)
        if not p.exists():
            print_error(f"Preset '{preset}' not found.")
            sys.exit(1)
        code = run_npx_skills(args, cwd=str(p))
        if code == 0:
            maybe_auto_push(f"remove {skill or 'skills'} from {preset}")
    else:
        if global_rm:
            args.append("-g")
        target_cwd = Path(project).expanduser().resolve() if project else Path.cwd()
        code = run_npx_skills(args, cwd=str(target_cwd))
    if code != 0:
        sys.exit(code)


def update(
    skill: Annotated[Optional[str], typer.Argument()] = None,
    preset: Annotated[
        Optional[str], typer.Option("--preset", "-P", help="Update skills inside this preset.")
    ] = None,
    global_update: Annotated[
        bool, typer.Option("--global", "-g", help="Update global skills.")
    ] = False,
    all_presets: Annotated[
        bool, typer.Option("--all", help="Update every preset with a skills-lock.json.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip prompt.")] = False,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency", "-j", help="Number of presets to update concurrently with --all."
        ),
    ] = 4,
) -> None:
    """Update skills to latest versions via npx skills update."""
    args = ["update"]
    if skill:
        args.append(skill)
    if yes:
        args.append("-y")

    if all_presets and (preset or global_update):
        print_error("--all cannot be combined with --preset or --global.")
        sys.exit(1)

    if all_presets:
        preset_dirs = (
            sorted(
                (path for path in PRESETS_DIR.iterdir() if path.is_dir()),
                key=lambda path: path.name,
            )
            if PRESETS_DIR.exists()
            else []
        )
        updateable = [path for path in preset_dirs if (path / "skills-lock.json").exists()]

        for path in preset_dirs:
            if path not in updateable:
                print_warn(f"Skipping preset '{path.name}': no skills-lock.json.")
        if not updateable:
            print_warn("No presets have a skills-lock.json to update.")
            return

        failed = []
        if len(updateable) <= 1 or concurrency <= 1:
            for path in updateable:
                print_header(f"Updating preset '{path.name}'")
                if run_npx_skills(args, cwd=str(path)) != 0:
                    failed.append(path.name)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            workers = min(len(updateable), max(1, concurrency))
            print_header(f"Updating {len(updateable)} presets concurrently ({workers} workers)")

            def update_one(p: Path) -> tuple[str, int]:
                return p.name, run_npx_skills(args, cwd=str(p))

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(update_one, path): path for path in updateable}
                for fut in as_completed(futures):
                    p_name, code = fut.result()
                    if code != 0:
                        failed.append(p_name)
                    else:
                        print_success(f"Preset '{p_name}' updated.")

        if len(failed) != len(updateable):
            maybe_auto_push("update all presets")
        if failed:
            print_error(
                f"Could not update preset{'s' if len(failed) != 1 else ''}: {', '.join(failed)}."
            )
            sys.exit(1)
        return

    if preset:
        p = preset_path(preset)
        if not p.exists():
            print_error(f"Preset '{preset}' not found.")
            sys.exit(1)
        # `npx skills update` scopes itself by the skills-lock.json in its cwd and
        # silently updates *global* skills when there is none. Presets built before
        # skctl wrote a lockfile have no such file, so refuse rather than let the
        # update drift somewhere the user never asked for.
        if not (p / "skills-lock.json").exists():
            print_error(
                f"Preset '{preset}' has no skills-lock.json, so there is nothing to "
                f"update against. Re-add its skills with "
                f"`skctl add <pkg> --preset {preset}` to record their sources."
            )
            sys.exit(1)
        code = run_npx_skills(args, cwd=str(p))
        if code == 0:
            maybe_auto_push(f"update {preset}")
    else:
        if global_update:
            args.append("-g")
        code = run_npx_skills(args, cwd=str(Path.cwd()))
    if code != 0:
        sys.exit(code)


def info(
    skill: Annotated[str, typer.Argument(help="Name of the skill to inspect.")],
    preset: Annotated[
        Optional[str], typer.Option("--preset", "-P", help="Inspect skill in a specific preset.")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option(
            "--project",
            "-p",
            help="Inspect skill for a specific project directory (default: current directory).",
        ),
    ] = None,
    global_scope: Annotated[
        bool, typer.Option("--global", "-g", help="Inspect global installation.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output machine-readable JSON.")
    ] = False,
) -> None:
    """Show details, description, and installation status of a skill."""
    import json
    import os

    from skill_ctl.catalog import format_relative_time
    from skill_ctl.picker import parse_skill_meta

    presets_found: list[dict] = []
    if preset:
        p = preset_path(preset)
        if not p.exists():
            print_error(f"Preset '{preset}' not found.")
            sys.exit(1)
        preset_skills = get_preset_skills(p)
        if skill not in preset_skills:
            print_error(f"Skill '{skill}' not found in preset '{preset}'.")
            sys.exit(1)
        presets_found.append({"name": preset, "path": str(preset_skills[skill])})
    elif not global_scope:
        if PRESETS_DIR.is_dir():
            try:
                for entry in sorted(PRESETS_DIR.iterdir()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        ps = get_preset_skills(entry)
                        if skill in ps:
                            presets_found.append({"name": entry.name, "path": str(ps[skill])})
            except OSError:
                pass

    project_data = None
    target_root = Path(project).expanduser().resolve() if project else Path.cwd()
    if not preset and not global_scope:
        project_skills = scan_installed_skills(target_root, is_global=False)
        matching = [s for s in project_skills if s["name"] == skill]
        if matching:
            match = matching[0]
            p_path = Path(match["path"])
            is_sym = p_path.is_symlink()
            target_sym = str(p_path.resolve()) if is_sym else None
            project_data = {
                "root": str(target_root),
                "path": str(p_path),
                "is_symlink": is_sym,
                "target": target_sym,
                "agents": sorted(match["agents"])
                if isinstance(match["agents"], (set, list))
                else match["agents"],
                "source": match.get("source"),
            }

    global_data = None
    if not preset:
        global_skills = scan_installed_skills(Path.home(), is_global=True)
        matching = [s for s in global_skills if s["name"] == skill]
        if matching:
            match = matching[0]
            g_path = Path(match["path"])
            is_sym = g_path.is_symlink()
            target_sym = str(g_path.resolve()) if is_sym else None
            global_data = {
                "path": str(g_path),
                "is_symlink": is_sym,
                "target": target_sym,
                "agents": sorted(match["agents"])
                if isinstance(match["agents"], (set, list))
                else match["agents"],
                "source": match.get("source"),
            }

    if global_scope and not global_data:
        print_error(f"Skill '{skill}' not found in global scope.")
        sys.exit(1)

    if not preset and not presets_found and not project_data and not global_data:
        print_error(f"Skill '{skill}' not found in presets, current project, or global scope.")
        sys.exit(1)

    # Determine canonical directory to read files and metadata
    if presets_found:
        canonical_dir = Path(presets_found[0]["path"])
    elif project_data:
        canonical_dir = Path(project_data["path"]).resolve()
    else:
        canonical_dir = Path(global_data["path"]).resolve()

    canonical_name, description, updated_at = parse_skill_meta(canonical_dir)
    if not description and (canonical_dir / "SKILL.md").is_file():
        try:
            content = (canonical_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("---"):
                    description = line[:120]
                    break
        except OSError:
            pass

    updated_str = None
    if updated_at:
        try:
            updated_str = format_relative_time(updated_at)
        except Exception:
            updated_str = str(updated_at)

    file_list = []
    total_size = 0
    if canonical_dir.is_dir():
        try:
            for root, _, files in os.walk(canonical_dir):
                for f in sorted(files):
                    full = Path(root) / f
                    try:
                        rel = full.relative_to(canonical_dir).as_posix()
                        if not rel.startswith(".git"):
                            file_list.append(rel)
                            total_size += full.stat().st_size
                    except (OSError, ValueError):
                        pass
        except OSError:
            pass

    if total_size < 1024:
        size_str = f"{total_size} B"
    elif total_size < 1024 * 1024:
        size_str = f"{total_size / 1024:.1f} KB"
    else:
        size_str = f"{total_size / (1024 * 1024):.1f} MB"

    source = None
    if project_data and project_data.get("source"):
        source = project_data["source"]
    elif presets_found:
        source = f"preset ({presets_found[0]['name']})"
    elif global_data and global_data.get("source"):
        source = global_data["source"]
    else:
        source = "local"

    if json_output:
        info_payload = {
            "name": skill,
            "description": description or None,
            "updated": updated_str,
            "source": source,
            "canonical_path": str(canonical_dir),
            "files": file_list,
            "total_bytes": total_size,
            "presets": presets_found,
            "project": project_data,
            "global": global_data,
        }
        print(json.dumps(info_payload, indent=2))
        return

    print_header(f"Skill: {skill}")
    if description:
        console.print(f"  [dim]Description:[/dim]  {description}")
    if updated_str:
        console.print(f"  [dim]Updated:[/dim]      {updated_str}")
    if source:
        console.print(f"  [dim]Source:[/dim]       {source}")
    console.print(f"  [dim]Files:[/dim]        {len(file_list)} files ({size_str})")
    console.print(f"  [dim]Path:[/dim]         [path]{_display_path(canonical_dir)}[/path]")

    console.print("")
    console.print("[bold]Installation Status:[/bold]")

    if presets_found:
        p_names = ", ".join(f"[choice]{p['name']}[/choice]" for p in presets_found)
        console.print(f"  [dim]Presets:[/dim]  {p_names}")
    else:
        console.print("  [dim]Presets:[/dim]  [dim](none)[/dim]")

    if project_data:
        agents_str = _format_agents_summary(project_data["agents"])
        loc_str = _display_path(project_data["path"])
        if project_data.get("is_symlink") and project_data.get("target"):
            loc_str += f" -> [dim]{_display_path(project_data['target'])}[/dim]"
        console.print(f"  [dim]Project:[/dim]  [path]{_display_path(project_data['root'])}[/path]")
        console.print(f"    • Agents: {agents_str}")
        console.print(f"    • Link:   [path]{loc_str}[/path]")
    else:
        console.print("  [dim]Project:[/dim]  [dim](not installed in current project)[/dim]")

    if global_data:
        agents_str = _format_agents_summary(global_data["agents"])
        loc_str = _display_path(global_data["path"])
        if global_data.get("is_symlink") and global_data.get("target"):
            loc_str += f" -> [dim]{_display_path(global_data['target'])}[/dim]"
        console.print("  [dim]Global:[/dim]")
        console.print(f"    • Agents: {agents_str}")
        console.print(f"    • Link:   [path]{loc_str}[/path]")
    else:
        console.print("  [dim]Global:[/dim]   [dim](not installed globally)[/dim]")
