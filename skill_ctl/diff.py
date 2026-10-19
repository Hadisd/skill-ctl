"""Directory and preset diff comparison logic and CLI command."""

import difflib
import json as jsonlib
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.syntax import Syntax

from skill_ctl.config import load_config
from skill_ctl.constants import PRESETS_DIR
from skill_ctl.registry import presets_for
from skill_ctl.skillscan import get_preset_skills
from skill_ctl.ui import console, print_error, print_header, print_success


def _preset_path(preset_name: str) -> Path:
    name = str(preset_name)
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        print_error(f"Invalid preset name '{name}': must be a single directory name.")
        sys.exit(1)
    return PRESETS_DIR / name


def diff_directories(
    source_dir: Path,
    target_dir: Path,
    source_label: str = "source",
    target_label: str = "target",
) -> dict:
    """Compare contents of two directories recursively."""

    def _walk(d: Path) -> dict[str, Path]:
        res = {}
        if not d.is_dir():
            return res
        for root, _, files in os.walk(d):
            for f in files:
                full = Path(root) / f
                try:
                    rel = full.relative_to(d).as_posix()
                    if not rel.startswith(".git"):
                        res[rel] = full
                except (OSError, ValueError):
                    pass
        return res

    src_map = _walk(source_dir)
    tgt_map = _walk(target_dir)

    common = sorted(set(src_map.keys()) & set(tgt_map.keys()))
    added = sorted(set(tgt_map.keys()) - set(src_map.keys()))
    deleted = sorted(set(src_map.keys()) - set(tgt_map.keys()))
    modified = []

    for rel in common:
        f_src = src_map[rel]
        f_tgt = tgt_map[rel]
        try:
            b_src = f_src.read_bytes()
            b_tgt = f_tgt.read_bytes()
        except OSError:
            continue

        if b_src == b_tgt:
            continue

        is_binary = (b"\0" in b_src[:1024]) or (b"\0" in b_tgt[:1024])
        if is_binary:
            modified.append({
                "path": rel,
                "binary": True,
                "added": 0,
                "deleted": 0,
                "diff": [],
            })
        else:
            s_lines = b_src.decode("utf-8", errors="replace").splitlines(keepends=True)
            t_lines = b_tgt.decode("utf-8", errors="replace").splitlines(keepends=True)
            diff_lines = list(difflib.unified_diff(
                s_lines, t_lines,
                fromfile=f"{source_label}/{rel}",
                tofile=f"{target_label}/{rel}",
            ))
            added_cnt = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
            deleted_cnt = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
            modified.append({
                "path": rel,
                "binary": False,
                "added": added_cnt,
                "deleted": deleted_cnt,
                "diff": diff_lines,
            })

    identical = not (modified or added or deleted)
    return {
        "identical": identical,
        "modified": modified,
        "added": added,
        "deleted": deleted,
    }


def diff_presets(
    preset1_name: str,
    preset2_name: str,
    patch: bool = False,
    json_output: bool = False,
    exit_code: bool = False,
) -> None:
    """Compare two presets."""
    p1 = _preset_path(preset1_name)
    p2 = _preset_path(preset2_name)
    if not p1.is_dir():
        print_error(f"Preset '{preset1_name}' does not exist.")
        sys.exit(1)
    if not p2.is_dir():
        print_error(f"Preset '{preset2_name}' does not exist.")
        sys.exit(1)

    s1 = get_preset_skills(p1)
    s2 = get_preset_skills(p2)

    all_skills = sorted(set(s1.keys()) | set(s2.keys()))
    results = {}
    has_diff = False

    for s_name in all_skills:
        if s_name not in s1:
            results[s_name] = {"status": "added_in_target", "details": None}
            has_diff = True
        elif s_name not in s2:
            results[s_name] = {"status": "removed_in_target", "details": None}
            has_diff = True
        else:
            diff_res = diff_directories(
                s1[s_name], s2[s_name],
                source_label=f"{preset1_name}/{s_name}",
                target_label=f"{preset2_name}/{s_name}",
            )
            if not diff_res["identical"]:
                has_diff = True
                results[s_name] = {"status": "modified", "details": diff_res}
            else:
                results[s_name] = {"status": "identical", "details": diff_res}

    if json_output:
        payload = {
            "source_preset": preset1_name,
            "target_preset": preset2_name,
            "identical": not has_diff,
            "skills": results,
        }
        print(jsonlib.dumps(payload, indent=2))
        if exit_code and has_diff:
            sys.exit(1)
        return

    print_header(f"Diff: preset '{preset1_name}' vs '{preset2_name}'")
    if not has_diff:
        print_success(f"Presets '{preset1_name}' and '{preset2_name}' are identical.")
        return

    for s_name, res in results.items():
        st = res["status"]
        if st == "added_in_target":
            console.print(f"  [choice]{s_name}[/choice]: [success]only in '{preset2_name}'[/success]")
        elif st == "removed_in_target":
            console.print(f"  [choice]{s_name}[/choice]: [error]only in '{preset1_name}'[/error]")
        elif st == "modified":
            console.print(f"  [choice]{s_name}[/choice]:")
            details = res["details"]
            for m in details["modified"]:
                if m["binary"]:
                    console.print(f"    • [warn]modified (binary):[/warn] {m['path']}")
                else:
                    console.print(f"    • [warn]modified:[/warn] {m['path']} [dim](+{m['added']}, -{m['deleted']})[/dim]")
            for a in details["added"]:
                console.print(f"    • [success]added in {preset2_name}:[/success] {a}")
            for d in details["deleted"]:
                console.print(f"    • [error]missing in {preset2_name}:[/error] {d}")
            if patch:
                for m in details["modified"]:
                    if m.get("diff"):
                        console.print(Syntax("".join(m["diff"]), "diff"))

    if exit_code and has_diff:
        sys.exit(1)


def diff_cmd(
    preset: Annotated[
        Optional[str],
        typer.Argument(help="Preset to compare against (default: applied preset).")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Project path to inspect (default: current directory).")
    ] = None,
    skill: Annotated[
        Optional[str],
        typer.Option("--skill", "-s", help="Specific skill to diff.")
    ] = None,
    patch: Annotated[
        bool,
        typer.Option("--patch", "-u", help="Show unified diffs of modified files.")
    ] = False,
    stat: Annotated[
        bool,
        typer.Option("--stat", help="Show diffstat summary of changes.")
    ] = False,
    exit_code: Annotated[
        bool,
        typer.Option("--exit-code", help="Exit with code 1 if differences are found.")
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output machine-readable JSON.")
    ] = False,
) -> None:
    """Show differences between project skills and a preset."""
    from skill_ctl.skills import scan_installed_skills

    target_project = Path(project).expanduser().resolve() if project else Path.cwd()

    if preset:
        preset_name = preset
    else:
        applied = presets_for(target_project)
        if applied:
            if len(applied) == 1:
                preset_name = next(iter(applied.keys()))
            elif skill:
                matching = [p for p in applied if skill in get_preset_skills(_preset_path(p))]
                preset_name = matching[0] if matching else next(iter(applied.keys()))
            else:
                preset_name = next(iter(applied.keys()))
        else:
            config = load_config()
            def_p = config.get("default_preset", "default")
            if _preset_path(def_p).is_dir():
                preset_name = def_p
            else:
                print_error("No preset specified and no presets applied to current project.")
                sys.exit(1)

    p_dir = _preset_path(preset_name)
    if not p_dir.is_dir():
        print_error(f"Preset '{preset_name}' does not exist.")
        sys.exit(1)

    preset_skills = get_preset_skills(p_dir)
    if skill:
        if skill not in preset_skills:
            print_error(f"Skill '{skill}' not found in preset '{preset_name}'.")
            sys.exit(1)
        preset_skills = {skill: preset_skills[skill]}

    installed = scan_installed_skills(target_project, is_global=False)
    installed_map: dict[str, Path] = {}
    for item in installed:
        if skill and item["name"] != skill:
            continue
        installed_map[item["name"]] = Path(item["path"])

    all_skill_names = sorted(set(preset_skills.keys()) | set(installed_map.keys()))
    results = {}
    has_diff = False

    for s_name in all_skill_names:
        if s_name not in preset_skills:
            results[s_name] = {"status": "untracked_in_preset", "details": None}
            has_diff = True
        elif s_name not in installed_map:
            results[s_name] = {"status": "not_installed", "details": None}
            has_diff = True
        else:
            src_path = preset_skills[s_name]
            tgt_path = installed_map[s_name]

            if tgt_path.is_symlink() and tgt_path.resolve() == src_path.resolve():
                results[s_name] = {"status": "symlinked", "details": None}
            else:
                diff_res = diff_directories(
                    src_path, tgt_path,
                    source_label=f"preset:{preset_name}/{s_name}",
                    target_label=f"project:{s_name}",
                )
                if not diff_res["identical"]:
                    has_diff = True
                    results[s_name] = {"status": "modified", "details": diff_res}
                else:
                    results[s_name] = {"status": "identical", "details": diff_res}

    if json_output:
        payload = {
            "preset": preset_name,
            "project": str(target_project),
            "identical": not has_diff,
            "skills": results,
        }
        print(jsonlib.dumps(payload, indent=2))
        if exit_code and has_diff:
            sys.exit(1)
        return

    print_header(f"Diff: project vs preset '{preset_name}'")
    if not has_diff:
        print_success(f"All skills match preset '{preset_name}'.")
        return

    for s_name, res in results.items():
        st = res["status"]
        if st == "symlinked":
            if stat:
                console.print(f"  [choice]{s_name}[/choice]: [dim]identical (symlinked)[/dim]")
        elif st == "identical":
            if stat:
                console.print(f"  [choice]{s_name}[/choice]: [dim]identical[/dim]")
        elif st == "not_installed":
            console.print(f"  [choice]{s_name}[/choice]: [error]not installed in project[/error]")
        elif st == "untracked_in_preset":
            console.print(f"  [choice]{s_name}[/choice]: [warn]only in project (not in preset)[/warn]")
        elif st == "modified":
            console.print(f"  [choice]{s_name}[/choice]:")
            details = res["details"]
            for m in details["modified"]:
                if m["binary"]:
                    console.print(f"    • [warn]modified (binary):[/warn] {m['path']}")
                else:
                    console.print(f"    • [warn]modified:[/warn] {m['path']} [dim](+{m['added']}, -{m['deleted']})[/dim]")
            for a in details["added"]:
                console.print(f"    • [success]added in project:[/success] {a}")
            for d in details["deleted"]:
                console.print(f"    • [error]missing in project:[/error] {d}")
            if patch:
                for m in details["modified"]:
                    if m.get("diff"):
                        console.print(Syntax("".join(m["diff"]), "diff"))

    if exit_code and has_diff:
        sys.exit(1)
