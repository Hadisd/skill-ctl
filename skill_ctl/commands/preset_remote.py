"""Remote preset retrieval, GitHub cloning, and zip archive import/export."""

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from skill_ctl.archive import export_preset as write_archive
from skill_ctl.archive import preset_changes, safe_extract_preset
from skill_ctl.backup import maybe_auto_push
from skill_ctl.config import load_config
from skill_ctl.constants import PRESETS_DIR, PREVIEW_TOGGLE_BIND, preview_window_options
from skill_ctl.picker import preview_markdown_command, wants_picker
from skill_ctl.skillscan import get_preset_skills
from skill_ctl.theme import fzf_color_arg, picker_ansi
from skill_ctl.ui import console, print_error, print_header, print_success, print_warn


def _preset_path(preset_name: str) -> Path:
    name = str(preset_name)
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        print_error(f"Invalid preset name '{name}': must be a single directory name.")
        sys.exit(1)
    return PRESETS_DIR / name


def preset_export(
    name: str,
    output: Optional[str] = None,
) -> None:
    """Write one preset, its files, and its source lock data to a zip archive."""
    source = _preset_path(name)
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
        target = _preset_path(target_name)
        changes = (
            preset_changes(target, extracted)
            if target.exists()
            else [
                ("add", path.relative_to(extracted))
                for path in extracted.rglob("*")
                if path.is_file()
            ]
        )
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
    if not repository or repository.startswith("-"):
        print_error(f"Invalid repository '{repository}': cannot start with '-'.")
        sys.exit(1)
    if "://" in repository or repository.startswith("git@") or Path(repository).exists():
        return repository
    if repository.count("/") == 1:
        return f"https://github.com/{repository}.git"
    print_error("GitHub repositories must be owner/repository or a clone URL.")
    sys.exit(1)


def remote_preset_preview(preset_dir: Path) -> str:
    """Show the useful parts of a downloaded preset without its temporary path."""
    skills = sorted(get_preset_skills(preset_dir))
    files = sorted(
        str(path.relative_to(preset_dir)) for path in preset_dir.rglob("*") if path.is_file()
    )
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
            rows.append(
                "\t".join(
                    (
                        str(index),
                        f"{colors['name']}{preset_dir.name:<22}{reset}",
                        f"{colors['description']}{len(get_preset_skills(preset_dir)):>6}{reset}",
                        str(preview),
                    )
                )
            )
        preview_cmd = preview_markdown_command("{4}")
        fzf_bin = shutil.which("fzf") or "fzf"
        is_cmd = sys.platform == "win32" and fzf_bin.lower().endswith((".cmd", ".bat"))
        try:
            result = subprocess.run(
                [
                    fzf_bin,
                    "--ansi",
                    "--multi",
                    "--tabstop",
                    "2",
                    "--delimiter",
                    "\t",
                    "--with-nth",
                    "2,3",
                    "--header",
                    "Preset                  Skills\nTab sel · ^A all / ^D none · ↵ import · ^/ view",
                    "--color",
                    fzf_color_arg(config.get("theme")),
                    "--bind",
                    f"ctrl-a:select-all,ctrl-d:deselect-all,{PREVIEW_TOGGLE_BIND}",
                    "--preview",
                    preview_cmd,
                    "--preview-window",
                    preview_window_options(),
                ],
                input="\n".join(rows),
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                shell=is_cmd,
            )
        except OSError:
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return [
            preset_dirs[int(row.split("\t", 1)[0]) - 1].name for row in result.stdout.splitlines()
        ]


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
            ["git", "clone", "--depth", "1", "--", github_repo_url(repository), str(clone)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print_error(
                (result.stderr or result.stdout).strip() or f"Could not download {repository}."
            )
            sys.exit(1)
        source_root = clone / "presets"
        available = (
            sorted(path for path in source_root.iterdir() if path.is_dir())
            if source_root.is_dir()
            else []
        )
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
            target = _preset_path(target_name)
            source = available_by_name[source_name]
            changes = (
                preset_changes(target, source)
                if target.exists()
                else [
                    ("add", path.relative_to(source))
                    for path in source.rglob("*")
                    if path.is_file()
                ]
            )
            print_header(f"Import preview: '{source_name}' -> '{target_name}'")
            for action, path in changes:
                console.print(f"  {action:6} {path}")
            if not changes:
                console.print("  [dim]no file changes[/dim]")
            if dry_run:
                continue
            if target.exists() and not replace:
                print_error(
                    f"Preset '{target_name}' already exists. Use --replace or --rename <name>."
                )
                continue
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            print_success(f"Imported preset '{target_name}' from {repository}")
            maybe_auto_push(f"import preset {target_name}")
