"""Direct skill commands wrapping npx skills (add, list, remove, update, find)."""

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.prompt import Confirm

from skill_ctl.constants import PRESETS_DIR
from skill_ctl.config import load_config
from skill_ctl.picker import DEFAULT_HEADER, pick, rows_for, wants_picker
from skill_ctl.ui import console, print_header, print_error, print_warn
from skill_ctl.prompts import prompt_add_source, prompt_destination, prompt_skills
from skill_ctl.runner import run_npx_skills, get_detected_global_agents
from skill_ctl.presets import add_installed_to_preset, ensure_preset_dir, get_preset_skills, preset_path
from skill_ctl.backup import maybe_auto_push

def add(
    package: Annotated[Optional[str], typer.Argument()] = None,
    preset: Annotated[
        Optional[str],
        typer.Option("--preset", "-P", help="Preset to install into (~/.skill-ctl/presets/<name>/).")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Project path to install directly into (default: current directory).")
    ] = None,
    global_install: Annotated[
        bool,
        typer.Option("--global", "-g", help="Install globally instead of project-level.")
    ] = False,
    skill: Annotated[
        Optional[str],
        typer.Option("--skill", "-s", help="Specific skill name to install.")
    ] = None,
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Target agent (e.g. universal, claude-code, cursor, or '*' for all).")
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompts.")
    ] = False,
    copy: Annotated[
        bool,
        typer.Option("--copy", "-c", help="Copy files instead of symlinking.")
    ] = False,
    all_skills: Annotated[
        bool,
        typer.Option("--all", help="Shorthand for --skill '*' --agent '*' -y.")
    ] = False,
    full_depth: Annotated[
        bool,
        typer.Option("--full-depth", help="Search all subdirectories for SKILL.md.")
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output machine-readable JSON.")
    ] = False,
) -> None:
    """Add a skill package using npx skills (skills.sh). Can target a preset, project, or global."""
    if package is None and not sys.stdin.isatty():
        print_error("Choosing a skill requires a terminal or a package name.")
        sys.exit(1)

    config = load_config()
    default_dest = config.get("default_destination", "prompt")
    ask_dest = config.get("prompts", {}).get("ask_destination", True)
    default_preset_name = config.get("default_preset", "default")
    # `add --preset <name>` has always meant “pick from installed skills”; only a
    # bare `add` needs help choosing between that and the remote catalog.
    add_source = prompt_add_source() if package is None and preset is None else "2"

    # If no destination flag was provided, determine target from config or prompt
    if preset is None and not global_install and project is None:
        if default_dest == "global":
            global_install = True
        elif default_dest == "project":
            project = "."
        elif default_dest == "preset":
            preset = default_preset_name
        elif ask_dest and sys.stdin.isatty() and not yes:
            preset, project_flag, global_install = prompt_destination(PRESETS_DIR, default_preset=default_preset_name)
            if project_flag:
                project = "."
            elif preset is None and not global_install:
                return
        else:
            preset = default_preset_name

    if package is None:
        if add_source == "1":
            find(preset=preset, global_install=global_install)
            return
        if not preset:
            print_error("Skills from other presets can only be added to a preset.")
            sys.exit(1)
        preset_dir = ensure_preset_dir(preset)
        if add_installed_to_preset(preset, preset_dir, config):
            maybe_auto_push(f"add installed skills to {preset}")
        return

    npx_args = [package]
    if skill:
        npx_args.extend(["--skill", skill])
    if agent:
        npx_args.extend(["--agent", agent])
    elif config.get("default_agents"):
        for a in config["default_agents"]:
            npx_args.extend(["--agent", a])

    # Only pass -y if explicitly requested
    if yes:
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

def list_skills(
    preset: Annotated[
        Optional[str],
        typer.Option("--preset", "-P", help="Preset to inspect skills for.")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Inspect skills for a specific project directory (default: current directory).")
    ] = None,
    global_list: Annotated[
        bool,
        typer.Option("--global", "-g", help="List global skills.")
    ] = False,
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Filter by agent (e.g. universal, claude-code, cursor).")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON.")
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
            print(f"  - {s}")
    else:
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

def remove(
    skill: Annotated[Optional[str], typer.Argument()] = None,
    preset: Annotated[
        Optional[str],
        typer.Option("--preset", "-P", help="Remove skill from this preset.")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Remove from a specific project directory (default: current directory).")
    ] = None,
    global_rm: Annotated[
        bool,
        typer.Option("--global", "-g", help="Remove skill from global scope.")
    ] = False,
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Remove from specific agent only (e.g. universal, claude-code, cursor).")
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt.")
    ] = False,
) -> None:
    """Remove an installed skill. Omit the name to pick interactively."""
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
                    default=False, console=console,
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
        Optional[str],
        typer.Option("--preset", "-P", help="Update skills inside this preset.")
    ] = None,
    global_update: Annotated[
        bool,
        typer.Option("--global", "-g", help="Update global skills.")
    ] = False,
    all_presets: Annotated[
        bool,
        typer.Option("--all", help="Update every preset with a skills-lock.json.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip prompt.")
    ] = False,
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
        preset_dirs = sorted(
            (path for path in PRESETS_DIR.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        ) if PRESETS_DIR.exists() else []
        updateable = [path for path in preset_dirs if (path / "skills-lock.json").exists()]

        for path in preset_dirs:
            if path not in updateable:
                print_warn(f"Skipping preset '{path.name}': no skills-lock.json.")
        if not updateable:
            print_warn("No presets have a skills-lock.json to update.")
            return

        failed = []
        for path in updateable:
            print_header(f"Updating preset '{path.name}'")
            if run_npx_skills(args, cwd=str(path)) != 0:
                failed.append(path.name)

        if len(failed) != len(updateable):
            maybe_auto_push("update all presets")
        if failed:
            print_error(f"Could not update preset{'s' if len(failed) != 1 else ''}: {', '.join(failed)}.")
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

def find(
    query: Annotated[Optional[str], typer.Argument()] = None,
    preset: Annotated[
        Optional[str],
        typer.Option("--preset", "-P", help="Install a selected skill into this preset.")
    ] = None,
    owner: Annotated[Optional[str], typer.Option("--owner", help="Search only repositories from a GitHub owner.")] = None,
    global_install: Annotated[
        bool,
        typer.Option("--global", "-g", help="Install the selected skill globally.")
    ] = False,
    npx: Annotated[
        bool,
        typer.Option("--npx", help="Use npx skills' own interactive finder instead of skctl's fzf picker.")
    ] = False,
) -> None:
    """Search skills.sh with skctl's picker.

    Each result shows its install count, GitHub stars, and a SKILL.md preview
    before you pick, and fzf allows multi-select. --npx uses npx skills' own
    finder instead. To search the skills you already have, use `skctl search`.
    """
    if not npx:
        # The catalog client pulls in HTTP and YAML support.  `find` is the only
        # command that needs it, so defer that startup work for every other CLI
        # command.
        from skill_ctl.catalog import choose as choose_catalog_skill, inspect as inspect_catalog_skill

        selected = choose_catalog_skill(query or "", owner)
        if not selected or not inspect_catalog_skill(selected):
            return

        # Several picks can come from different repos; one `npx skills add`
        # call per source, naming every skill from that source with its own
        # --skill flag installs them together instead of one call each.
        by_source: dict[str, list[str]] = {}
        for skill in selected:
            source = str(skill.get("source") or skill.get("id", ""))
            name = str(skill.get("name", ""))
            if source:
                by_source.setdefault(source, []).append("*" if skill.get("_all_from_source") else name)
        if not by_source:
            print_error("The selected skills.sh results have no installable source.")
            sys.exit(1)

        target = ensure_preset_dir(preset) if preset else Path.cwd()
        installed: list[str] = []
        exit_code = 0
        for source, names in by_source.items():
            args = ["add", source]
            if "*" not in names:
                for name in names:
                    args.extend(["--skill", name])
            if global_install:
                args.append("-g")
            code = run_npx_skills(args, cwd=str(target))
            if code == 0:
                installed.extend(names)
            else:
                exit_code = code
        if installed and preset:
            maybe_auto_push(f"add {', '.join(installed)} to {preset}")
        if exit_code != 0:
            sys.exit(exit_code)
        return

    args = ["find"] + ([query] if query else [])
    if owner:
        args.extend(["--owner", owner])
    if global_install:
        args.append("-g")
    target = ensure_preset_dir(preset) if preset else Path.cwd()
    code = run_npx_skills(args, cwd=str(target))
    if code == 0 and preset:
        maybe_auto_push(f"find skill for {preset}")
    if code != 0:
        sys.exit(code)
