"""Searching the skills you already have, across presets and global folders.

`find` asks skills.sh what exists; this asks your own presets what you have, which
is the question after a few dozen skills have piled up in half a dozen presets.
"""

import json as jsonlib
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from skill_ctl.config import load_config
from skill_ctl.constants import PRESETS_DIR
from skill_ctl.picker import DEFAULT_HEADER, pick, rows_for
from skill_ctl.presets import apply as apply_preset, get_global_skills, get_preset_skills, preset_path
from skill_ctl.registry import presets_for
from skill_ctl.ui import console, print_error, print_header, print_warn


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


def is_subsequence(term: str, text: str) -> bool:
    """True if every character of term appears in text, in order but not adjacent."""
    it = iter(text)
    return all(char in it for char in term)


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
    query: Annotated[Optional[str], typer.Argument()] = None,
    preset: Annotated[
        Optional[str],
        typer.Option("--preset", "-P", help="Search only this preset.")
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
        typer.Option("--global", "-g", help="Search only configured global skill folders.")
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON, with descriptions in full.")
    ] = False,
    pick_and_apply: Annotated[
        bool,
        typer.Option("--pick", "-i", help="Deprecated; search is already interactive.")
    ] = False,
) -> None:
    """Search installed preset and global skills by name and description.

    Opens fzf with each SKILL.md in a preview pane and applies what you select.
    A query starts fzf with those words entered. --json prints the results
    instead.

    `skctl find` searches skills.sh for skills you do not have yet.
    """
    rows = collect(preset, global_only)

    terms = (query or "").lower().split()
    if terms:
        scored = []
        for row in rows:
            name = f"{row['skill']} {row['name']}".lower()
            description = row["description"].lower()
            scores = [term_score(term, name, description) for term in terms]
            # Every term has to match something, as with fzf's own AND semantics.
            if all(score is not None for score in scores):
                row["score"] = sum(scores)
                scored.append(row)
        rows = scored

    target_project = Path(project).expanduser().resolve() if project else Path.cwd()
    here = presets_for(target_project)
    for row in rows:
        entry = here.get(row["preset"]) or {} if row.get("scope") == "preset" else {}
        row["applied"] = row.get("scope") == "global" or row["skill"] in entry.get("skills", [])
    if applied:
        rows = [row for row in rows if row["applied"]]

    # What matched decides the order: the name exactly, the name fuzzily, then only
    # the description. Matching is blunt enough that "ai" is inside "ponytail", so
    # the ranking is what keeps the row you meant at the top - and the top row is
    # what the footer offers to apply.
    rows.sort(key=lambda row: (row.get("score", 0.0), row["skill"], row["preset"]))

    if json_output:
        print(jsonlib.dumps(rows, indent=2))
        return

    if not rows:
        where = "global folders" if global_only else (f"preset '{preset}'" if preset else "your presets and global folders")
        print_warn(f"Nothing in {where} matches {query!r}." if terms else f"No skills in {where} yet.")
        console.print("Search skills.sh for new ones with: [header]skctl find <query>[/header]")
        return

    if not sys.stdin.isatty() and not pick_and_apply:
        print_error("skctl search is interactive; run it in a terminal or use --json.")
        sys.exit(1)

    header = (
        f"\x1b[1;36mSearch skills\x1b[0m  →  \x1b[2;32m{target_project}\x1b[0m\n"
        f"{DEFAULT_HEADER}"
    )
    chosen = pick(rows, header=header, query=query or "")
    if not chosen:
        print_warn("Nothing picked.")
        return
    apply_picked(chosen, project)
