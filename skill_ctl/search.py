"""Search skills across presets, global folders, and the remote skills.sh catalog."""


import json as jsonlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Optional

import typer

from skill_ctl.backup import maybe_auto_push
from skill_ctl.config import load_config
from skill_ctl.constants import PRESETS_DIR
from skill_ctl.picker import DEFAULT_HEADER, pick, rows_for
from skill_ctl.presets import (
    add_installed_to_global,
    apply as apply_preset,
    ensure_preset_dir,
    get_global_skills,
    get_preset_skills,
    preset_path,
)
from skill_ctl.registry import presets_for
from skill_ctl.runner import run_npx_skills
from skill_ctl.ui import console, print_error, print_header, print_success, print_warn


def is_subsequence(term: str, text: str) -> bool:
    """True if every character of term appears in text, in order but not adjacent."""
    it = iter(text)
    return all(char in it for char in term)


def match_span(term: str, text: str) -> Optional[int]:
    """How much of text the first greedy subsequence match spans. None if not a subsequence."""
    start = end = -1
    position = 0
    for char in term:
        position = text.find(char, position)
        if position < 0:
            return None
        if start < 0:
            start = position
        end = position
        position += 1
    return end - start + 1


def term_score(term: str, name: str, description: str) -> Optional[float]:
    """How well one query term matches, lowest first. None means it does not.

    Fuzzy matching is for the name only: `ltxpap` should find `latex-paper`, but a
    subsequence in a three-hundred-character description matches nearly everything,
    which would bury the results that count.
    """
    if term in name:
        return 0.0
    span = match_span(term, name)
    if span is not None:
        return 1.0 + span / max(len(name), 1)
    if term in description:
        return 3.0
    return None


def collect(preset: Optional[str], global_only: bool = False) -> list:
    """Skills in presets and configured global folders."""
    if preset and global_only:
        print_error("--preset and --global select different locations. Pick one.")
        sys.exit(1)
    if global_only:
        preset_dirs = []
    elif preset:
        directory = preset_path(preset)
        if not directory.is_dir():
            print_error(f"Preset '{preset}' not found at {directory}")
            sys.exit(1)
        preset_dirs = [directory]
    elif PRESETS_DIR.is_dir():
        preset_dirs = sorted(d for d in PRESETS_DIR.iterdir() if d.is_dir())
    else:
        preset_dirs = []

    rows = []
    for directory in preset_dirs:
        rows.extend(rows_for(directory.name, get_preset_skills(directory)))
    if not preset:
        rows.extend(get_global_skills(load_config()))
    return rows


def apply_picked(chosen: list, project: Optional[str]) -> None:
    """Apply what was picked, one `apply` per preset it came from."""
    by_preset = {}
    global_rows = [row for row in chosen if row.get("scope") == "global"]
    for row in chosen:
        if row.get("scope") == "global":
            continue
        by_preset.setdefault(row["preset"], []).append(row["skill"])

    if global_rows:
        print_warn(
            f"Skipped {len(global_rows)} global skill{'s' if len(global_rows) != 1 else ''}; "
            "they are already available globally."
        )

    for preset, names in sorted(by_preset.items()):
        print_header(f"Applying {len(names)} skill{'s' if len(names) != 1 else ''} from '{preset}'")
        apply_preset(preset=preset, skill=",".join(sorted(names)), project=project)


def search(
    query: Annotated[Optional[str], typer.Argument(help="Search term (skill name, keyword, or description).")] = None,
    preset: Annotated[
        Optional[str],
        typer.Option("--preset", "-P", help="Filter by preset, or install picked skills into this preset.")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Project to check against for what is applied (default: current directory).")
    ] = None,
    applied: Annotated[
        bool,
        typer.Option("--applied", help="Only skills already applied to the project.")
    ] = False,
    global_only: Annotated[
        bool,
        typer.Option("--global", "-g", help="Search only configured global skill folders (or install globally).")
    ] = False,
    local_only: Annotated[
        bool,
        typer.Option("--local", "-l", help="Only search local presets and global folders.")
    ] = False,
    remote: Annotated[
        Optional[str],
        typer.Option("--remote", "-r", help="Search remote catalogs (e.g. skills.sh, skillsmp, or all).")
    ] = None,
    owner: Annotated[
        Optional[str],
        typer.Option("--owner", help="Search only repositories from a GitHub owner (remote catalog).")
    ] = None,
    npx: Annotated[
        bool,
        typer.Option("--npx", help="Use npx skills' own interactive finder.")
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON, with descriptions in full.")
    ] = False,
    pick_and_apply: Annotated[
        bool,
        typer.Option("--pick", "-i", help="Deprecated; search is already interactive.")
    ] = False,
    remote_only: Annotated[
        Optional[bool],
        typer.Option(hidden=True)
    ] = None,
) -> None:
    """Search installed preset, global, and remote skills (skills.sh, skillsmp.com).

    Opens an interactive picker showing matching local and remote skills.
    Applying or installing what you select routes to the target project, preset,
    or global folders.
    """
    if npx:
        args = ["find"]
        if query:
            args.append(query)
        sys.exit(run_npx_skills(args))

    is_remote = bool(remote) or bool(remote_only)

    if local_only and is_remote:
        print_error("--local and --remote select different sources. Pick one.")
        sys.exit(1)

    if is_remote:
        remotes_filter = None
        if remote and str(remote).lower() not in ("default", "true", "1"):
            remotes_filter = [r.strip() for r in remote.split(",") if r.strip()]

        if json_output:
            from skill_ctl.catalog import search as catalog_search
            results = catalog_search(query or "", owner=owner, remotes=remotes_filter)
            print(jsonlib.dumps(results, indent=2))
            return

        if not sys.stdin.isatty():
            print_error("skctl search is interactive; run it in a terminal or use --json.")
            sys.exit(1)

        from skill_ctl.catalog import choose as choose_catalog_skill, inspect as inspect_catalog_skill

        selected = choose_catalog_skill(query or "", owner, remotes=remotes_filter)
        if not selected or not inspect_catalog_skill(selected):
            return

        action = selected[0].get("_action") if selected else None
        target_preset = preset
        is_global = global_only or (action == "alt-g")

        if action == "alt-p" and not target_preset:
            from skill_ctl.constants import PRESETS_DIR
            from skill_ctl.prompts import prompt_preset_or_new
            names = sorted(p.name for p in PRESETS_DIR.iterdir() if p.is_dir()) if PRESETS_DIR.is_dir() else []
            config = load_config()
            default_p = config.get("default_preset", "default")
            target_preset = prompt_preset_or_new(names, default_p, PRESETS_DIR)
            if not target_preset:
                print_warn("No preset selected; installation cancelled.")
                return

        by_source: dict[str, list[str]] = {}
        for skill in selected:
            source = str(skill.get("source") or skill.get("id", ""))
            name = str(skill.get("name", ""))
            if source:
                by_source.setdefault(source, []).append("*" if skill.get("_all_from_source") else name)
        if not by_source:
            print_error("The selected remote results have no installable source.")
            sys.exit(1)

        if is_global and sys.stdin.isatty():
            from rich.prompt import Confirm
            all_names = [n for names in by_source.values() for n in names if n != "*"] or [s.get("name", "") for s in selected]
            names_str = ", ".join(all_names)
            prompt_msg = f"Install {len(all_names)} remote skill{'s' if len(all_names) != 1 else ''} ({names_str}) globally (~/)?"
            try:
                if not Confirm.ask(prompt_msg, default=True, console=console):
                    print_warn("Installation cancelled.")
                    return
            except (EOFError, KeyboardInterrupt):
                console.print()
                return

        target = ensure_preset_dir(target_preset) if target_preset else (Path.home() if is_global else (Path(project).expanduser().resolve() if project else Path.cwd()))
        installed: list[str] = []
        exit_code = 0
        for source, names in by_source.items():
            args = ["add", source]
            if "*" not in names:
                for name in names:
                    args.extend(["--skill", name])
            if is_global:
                args.append("-g")
            code = run_npx_skills(args, cwd=str(target))
            if code == 0:
                installed.extend(names)
            else:
                exit_code = code
        if installed and target_preset:
            maybe_auto_push(f"add {', '.join(installed)} to {target_preset}")
            print_success(f"Added {len(installed)} skill{'s' if len(installed) != 1 else ''} to preset '{target_preset}'.")
        elif installed and is_global:
            print_success(f"Installed {len(installed)} skill{'s' if len(installed) != 1 else ''} globally.")
        if exit_code != 0:
            sys.exit(exit_code)
        return

    terms = (query or "").lower().split()
    target_project = Path(project).expanduser().resolve() if project else Path.cwd()

    local_rows = collect(preset, global_only)
    if terms:
        scored = []
        for row in local_rows:
            name = f"{row['skill']} {row['name']}".lower()
            description = row.get("description", "").lower()
            scores = [term_score(term, name, description) for term in terms]
            if all(score is not None for score in scores):
                row["score"] = sum(scores)
                scored.append(row)
        local_rows = scored

    here = presets_for(target_project)
    for row in local_rows:
        entry = here.get(row["preset"]) or {} if row.get("scope") == "preset" else {}
        row["applied"] = row.get("scope") == "global" or row["skill"] in entry.get("skills", [])
        row["source_type"] = "local"
    if applied:
        local_rows = [row for row in local_rows if row.get("applied")]

    local_rows.sort(key=lambda row: (row.get("score", 0.0), row["skill"], row["preset"]))

    if json_output:
        print(jsonlib.dumps(local_rows, indent=2))
        return

    if not local_rows:
        where = "global folders" if global_only else (f"preset '{preset}'" if preset else "your presets and global folders")
        print_warn(f"Nothing in {where} matches {query!r}." if terms else f"No skills in {where} yet.")
        console.print("Search skills.sh for new ones with: [header]skctl search --remote <query>[/header]")
        return

    if not sys.stdin.isatty() and not pick_and_apply:
        print_error("skctl search is interactive; run it in a terminal or use --json.")
        sys.exit(1)

    header = (
        f"\x1b[1;36mSearch skills\x1b[0m  →  \x1b[2;32m{target_project}\x1b[0m\n"
        f"{DEFAULT_HEADER}"
    )
    chosen = pick(local_rows, header=header, query=query or "")
    if not chosen:
        print_warn("Nothing picked.")
        return

    action = chosen[0].get("_action") if chosen else None

    if action == "alt-p" or (preset and action != "alt-g"):
        target_preset = preset
        if not target_preset:
            from skill_ctl.constants import PRESETS_DIR
            from skill_ctl.prompts import prompt_preset_or_new
            names = sorted(p.name for p in PRESETS_DIR.iterdir() if p.is_dir()) if PRESETS_DIR.is_dir() else []
            config = load_config()
            default_p = config.get("default_preset", "default")
            target_preset = prompt_preset_or_new(names, default_p, PRESETS_DIR)
            if not target_preset:
                print_warn("No preset selected; cancelled.")
                return

        from skill_ctl.presets import install_skills_into_preset
        target_dir = ensure_preset_dir(target_preset)
        added = install_skills_into_preset(target_preset, target_dir, chosen)
        if added:
            maybe_auto_push(f"add {', '.join(r['skill'] for r in chosen)} to {target_preset}")
    elif action == "alt-g" or global_only:
        add_installed_to_global(load_config(), skill=",".join(r["skill"] for r in chosen))
    else:
        apply_picked(chosen, project=str(target_project))
