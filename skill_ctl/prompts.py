import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from rich.prompt import Prompt

from skill_ctl.constants import ALL_PRESETS
from skill_ctl.config import load_config
from skill_ctl.registry import projects_using
from skill_ctl.theme import bat_theme, fzf_color_arg
from skill_ctl.ui import console, print_error, print_warn

ALL_SKILLS = "0"


def _preview_markdown_command(field_placeholder: str = "{3}") -> str:
    """Render markdown preview using bat or glow when available, falling back to cat/type."""
    width = '"%FZF_PREVIEW_COLUMNS%"' if sys.platform == "win32" else '"$FZF_PREVIEW_COLUMNS"'
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
    command = "type" if sys.platform == "win32" else "cat"
    return f"{command} {field_placeholder}"


def _ask(label: str, **kwargs: object) -> str:
    try:
        return Prompt.ask(label, console=console, **kwargs).strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        raise SystemExit(0)


def _use_preset_picker(count: int, config: dict) -> bool:
    picker_mode = str(config.get("prompts", {}).get("picker", "auto")).lower()
    try:
        threshold = max(0, int(config.get("prompts", {}).get("preset_picker_threshold", 10)))
    except (TypeError, ValueError):
        threshold = 10
    return (
        count > threshold
        and shutil.which("fzf")
        and picker_mode not in ("numbers", "number", "none", "false")
        and (picker_mode == "fzf" or sys.stdin.isatty())
    )


def _browse_preset_choices(
    choices: list[str], config: dict, presets_dir: Optional[Path], extra: tuple[str, str], default_preset: Optional[str] = None,
) -> Optional[int]:
    with tempfile.TemporaryDirectory(prefix="skctl-preset-picker-") as temporary:
        previews = Path(temporary)
        extra_preview = previews / "extra"
        extra_text = extra[1]
        if not extra_text.startswith("#"):
            extra_text = f"# {extra[0]}\n\n{extra_text}"
        extra_preview.write_text(extra_text.strip() + "\n", encoding="utf-8")
        rows = [f"0\t{extra[0]}\t{extra_preview}"]
        for index, name in enumerate(choices, 1):
            preset_dir = presets_dir / name if presets_dir else None
            skills = sorted(path.parent.name for path in preset_dir.rglob("SKILL.md")) if preset_dir else []
            projects = projects_using(name)
            preview = previews / str(index)
            md = [
                f"# {name}",
                "",
                f"**Location:** `{preset_dir}`" if preset_dir else "",
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

            preview.write_text("\n".join(md).strip() + "\n", encoding="utf-8")
            marker = " *" if name == default_preset else ""
            rows.append(f"{index}\t{name}{marker}\t{preview}")
        preview_command = _preview_markdown_command("{3}")
        result = subprocess.run(
            [
                "fzf", "--delimiter", "\t", "--with-nth", "2",
                "--header", "Enter choose · Esc cancel",
                "--color", fzf_color_arg(config.get("theme")),
                "--preview", preview_command, "--preview-window", "right,55%,wrap",
            ],
            input="\n".join(rows), stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        return int(result.stdout.partition("\t")[0]) if result.returncode == 0 else None


def prompt_apply_destination(target_project: Path) -> str:
    """Ask whether to apply skills to the current project or globally."""
    console.print()
    console.print("[dim]Where would you like to apply?[/dim]")
    try:
        proj_label = "~/" + target_project.relative_to(Path.home()).as_posix() if target_project.is_relative_to(Path.home()) else str(target_project)
    except Exception:
        proj_label = str(target_project)
    console.print(f"  [choice]1)[/choice] Current project [dim]({proj_label}; default)[/dim]")
    console.print("  [choice]2)[/choice] Global [dim](~/.agents/skills, ~/.claude/skills, ...)[/dim]")
    return _ask("[bold]Choose destination[/bold]", choices=["1", "2"], default="1")


def prompt_apply_type() -> str:
    console.print()
    console.print("[dim]What would you like to apply?[/dim]")
    console.print("  [choice]1)[/choice] Specific skill(s) [dim](search & pick across all presets; default)[/dim]")
    console.print("  [choice]2)[/choice] An entire preset [dim](apply all skills from a preset)[/dim]")
    return _ask("[bold]Choose an option[/bold]", choices=["1", "2"], default="1")


def prompt_preset(
    names: list[str],
    target_project: Optional[Path] = None,
    presets_dir: Optional[Path] = None,
    include_all: bool = True,
    default_name: Optional[str] = None,
) -> str:
    config = load_config()
    default_choice = "0" if include_all else "1"
    if default_name and default_name in names and not include_all:
        default_choice = str(names.index(default_name) + 1)

    if _use_preset_picker(len(names), config):
        extra = ("all presets", "Search every skill across all presets.") if include_all else ("cancel", "Cancel selection.")
        selected = _browse_preset_choices(
            names, config, presets_dir, extra, default_name,
        )
        if selected is None:
            raise SystemExit(0)
        if selected == 0:
            if include_all:
                return ALL_PRESETS
            raise SystemExit(0)
        return names[selected - 1]

    if include_all:
        console.print("  [header]0)[/header] [header]all presets[/header] [dim](search every skill; default)[/dim]")
    for index, name in enumerate(names, 1):
        marker = " [dim]*[/dim]" if name == default_name else ""
        console.print(f"  [choice]{index})[/choice] {name}{marker}")
    if target_project is not None:
        console.print(f"[dim]Applying to[/dim] [path]{target_project}[/path]")
    while True:
        picked = _ask("[bold]Preset to apply[/bold]", default=default_choice)
        if picked == "0" and include_all:
            return ALL_PRESETS
        if picked.isdigit() and 1 <= int(picked) <= len(names):
            return names[int(picked) - 1]
        if picked in names:
            return picked
        valid = f"0, a number from 1 to {len(names)}" if include_all else f"a number from 1 to {len(names)}"
        print_warn(f"No preset '{picked}'. Pick {valid}, or a name.")


def prompt_skills(names: list[str], action: str = "apply") -> list[str]:
    console.print(
        f"  [header]{ALL_SKILLS})[/header] [header]all skills[/header]"
        f" [dim]({len(names)})[/dim] [dim](default)[/dim]"
    )
    for index, name in enumerate(names, 1):
        console.print(f"  [choice]{index})[/choice] {name}")
    tokens = _ask(f"[bold]Skills to {action}[/bold]", default=ALL_SKILLS).replace(",", " ").split()
    if not tokens or ALL_SKILLS in tokens:
        return names
    chosen = [names[int(token) - 1] if token.isdigit() and 1 <= int(token) <= len(names) else token for token in tokens]
    chosen = [name for name in chosen if name in names]
    if not chosen:
        print_error("No known skills selected; nothing to do.")
        raise SystemExit(1)
    return chosen


def prompt_preset_or_new(
    names: list[str], default_preset: str = "default", presets_dir: Optional[Path] = None,
) -> Optional[str]:
    choices = [default_preset] + [name for name in names if name != default_preset]
    config = load_config()
    if _use_preset_picker(len(choices), config):
        selected = _browse_preset_choices(
            choices, config, presets_dir, ("Create new preset", "Create an empty preset, then name it."), default_preset,
        )
        if selected is None:
            return None
        return prompt_new_preset_name() if selected == 0 else choices[selected - 1]

    console.print(f"  [header]0)[/header] [path]Create new preset[/path]")
    for index, name in enumerate(choices, 1):
        if name == default_preset:
            console.print(f"  [choice]{index})[/choice] {name} [dim]*[/dim]")
        else:
            console.print(f"  [choice]{index})[/choice] {name}")
    default_index = "1"
    while True:
        picked = _ask("[bold]Preset (number or name)[/bold]", default=default_index)
        if not picked.isdigit():
            if picked:
                return picked
            continue
        index = int(picked)
        if index == 0:
            return prompt_new_preset_name()
        if 1 <= index <= len(choices):
            return choices[index - 1]
        print_warn(f"Pick a number from 0 to {len(choices)}, or a name.")



def prompt_new_preset_name() -> str:
    while True:
        name = _ask("[bold]Enter preset name[/bold]")
        if name:
            return name
        print_warn("Preset name cannot be empty.")


def prompt_destination(
    presets_dir: Path,
    default_preset: str = "default",
    target_project: Optional[Path] = None,
) -> tuple[Optional[str], bool, bool]:
    presets_dir.mkdir(parents=True, exist_ok=True)
    names = sorted(path.name for path in presets_dir.iterdir() if path.is_dir())
    proj = target_project or Path.cwd()
    try:
        proj_label = "~/" + proj.relative_to(Path.home()).as_posix() if proj.is_relative_to(Path.home()) else str(proj)
    except Exception:
        proj_label = str(proj)

    console.print()
    console.print("[dim]Where would you like to install?[/dim]")
    console.print(f"  [choice]1)[/choice] Current project [dim]({proj_label}; default)[/dim]")
    console.print(f"  [choice]2)[/choice] Preset [dim](~/.skill-ctl/presets/...)[/dim]")
    console.print("  [choice]3)[/choice] Global [dim](~/.agents/skills, ~/.claude/skills, ...)[/dim]")
    choice = _ask("[bold]Choose destination[/bold]", choices=["1", "2", "3"], default="1")
    if choice == "1":
        return None, True, False
    if choice == "2":
        return prompt_preset_or_new(names, default_preset, presets_dir), False, False
    return None, False, True


def prompt_theme(names: list[str], current: str) -> str:
    for index, name in enumerate(names, 1):
        marker = "[success]*[/success]" if name == current else " "
        console.print(f" {marker} [choice]{index})[/choice] [header]{name}[/header]")
    default = str(names.index(current) + 1) if current in names else "1"
    while True:
        picked = _ask("[bold]Theme (number or name)[/bold]", default=default)
        if picked.isdigit() and 1 <= int(picked) <= len(names):
            return names[int(picked) - 1]
        if picked in names:
            return picked
        print_warn(f"Pick a number from 1 to {len(names)}, or a name.")


def prompt_add_source() -> str:
    console.print("[dim]No skill package provided. What do you want to do?[/dim]")
    console.print("  [choice]1)[/choice] Find on skills.sh")
    console.print("  [choice]2)[/choice] Pick from presets")
    return _ask("[bold]Choose an option[/bold]", choices=["1", "2"], default="1")
