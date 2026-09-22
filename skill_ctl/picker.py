"""Choosing skills from a list, in fzf when it can run (picker).

Both `search` and the skill question in `apply` end up here. It knows nothing
about presets beyond the rows handed to it, which is what lets `presets` use it
without the two modules importing each other.
"""

import atexit
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from skill_ctl.catalog import format_relative_time
from skill_ctl.constants import BASE_DIR
from skill_ctl.config import load_config
from skill_ctl.theme import bat_theme, fzf_color_arg, picker_ansi
from skill_ctl.ui import print_warn
from skill_ctl.prompts import prompt_skills

# Descriptions are truncated for fzf, whose lines cannot wrap. The preview pane
# shows the whole SKILL.md anyway.
PICK_DESC_LIMIT = 160
PICK_NAME_WIDTH = 22
PICK_LOCATION_WIDTH = 12
PICK_UPDATED_WIDTH = 10
PICK_DESC_WIDTH = 38

# Keys worth knowing, spelled out rather than left to be discovered.
DEFAULT_HEADER = (
    f"{'Skill':<{PICK_NAME_WIDTH}}  {'Location':<{PICK_LOCATION_WIDTH}}  {'Updated':<{PICK_UPDATED_WIDTH}}  Description\n"
    "Tab select · ctrl-a all · Enter apply · Alt-P add to preset · Alt-G global"
)


def truncate(value: object, width: int) -> str:
    text = str(value or "").replace("\t", " ")
    return text if len(text) <= width else text[:width - 1] + "…"


def preview_command() -> str:
    """Use an installed Markdown renderer, with plain text as the fallback."""
    skill_file = r"{5}\SKILL.md" if os.name == "nt" else "{5}/SKILL.md"
    width = '"%FZF_PREVIEW_COLUMNS%"' if os.name == "nt" else '"$FZF_PREVIEW_COLUMNS"'
    theme = shlex.quote(bat_theme(load_config().get("theme")))
    bat_options = (
        f'--color=always --paging=never --style=plain --language=md --theme={theme} '
        f'--squeeze-blank --wrap=character --terminal-width={width}'
    )
    if shutil.which("bat"):
        return f"bat {bat_options} {skill_file}"
    if shutil.which("batcat"):
        return f"batcat {bat_options} {skill_file}"
    if shutil.which("glow"):
        return f"glow --style dark {skill_file}"
    command = "type" if os.name == "nt" else "cat"
    return f"{command} {skill_file}"


_METADATA_CACHE: Optional[dict] = None
_METADATA_CACHE_DIRTY = False
METADATA_CACHE_FILE = BASE_DIR / ".metadata_cache.json"


def _load_metadata_cache() -> dict:
    global _METADATA_CACHE
    if _METADATA_CACHE is not None:
        return _METADATA_CACHE
    if METADATA_CACHE_FILE.is_file():
        try:
            data = json.loads(METADATA_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _METADATA_CACHE = data
                return _METADATA_CACHE
        except Exception:
            pass
    _METADATA_CACHE = {}
    return _METADATA_CACHE


def _save_metadata_cache() -> None:
    global _METADATA_CACHE_DIRTY
    if not _METADATA_CACHE_DIRTY or _METADATA_CACHE is None:
        return
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = METADATA_CACHE_FILE.with_suffix(".tmp")
        temp_file.write_text(json.dumps(_METADATA_CACHE, separators=(",", ":")), encoding="utf-8")
        temp_file.replace(METADATA_CACHE_FILE)
        _METADATA_CACHE_DIRTY = False
    except OSError:
        pass


atexit.register(_save_metadata_cache)


def parse_skill_meta(skill_dir: Path) -> tuple[str, str, Optional[float | str]]:
    """Parse name, description, and optional updated date from a skill's SKILL.md frontmatter."""
    name, description, updated_at = skill_dir.name, "", None
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return name, description, updated_at
    if not text.startswith("---"):
        return name, description, updated_at

    _, _, rest = text.partition("\n")
    block, sep, _ = rest.partition("\n---")
    if not sep:
        return name, description, updated_at

    # Search reads this metadata for every installed skill. PyYAML is much more
    # work than the scalar fields need, especially across large presets.
    values = {}
    needs_yaml = False
    lines = block.splitlines()
    for index, line in enumerate(lines):
        key, marker, value = line.partition(":")
        if not marker or key not in ("name", "description", "updated", "updated_at", "date"):
            continue
        value = value.strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            continuation = []
            for next_line in lines[index + 1:]:
                if next_line and not next_line[:1].isspace():
                    break
                continuation.append(next_line.strip())
            value = " ".join(continuation)
        elif key == "description" and value[:1] in "\"'":
            # Quoted YAML can contain escapes or span several lines. Those are
            # uncommon, so keep PyYAML as the accurate fallback.
            needs_yaml = True
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value

    if needs_yaml:
        try:
            import yaml
            values = yaml.safe_load(block)
        except Exception:
            return name, description, updated_at
        if not isinstance(values, dict):
            return name, description, updated_at

    name = values.get("name") or name
    description = " ".join(str(values.get("description", "")).split())
    updated_at = values.get("updated") or values.get("updated_at") or values.get("date")
    return name, description, updated_at


def skill_meta(skill_dir: Path) -> tuple[str, str, Optional[float | str]]:
    """The name, description, and update timestamp from a skill's SKILL.md frontmatter/mtime.

    Falls back to the directory name, no description, and file mtime.
    Uses an mtime/size-validated cache for fast repeat searches.
    """
    global _METADATA_CACHE_DIRTY
    skill_file = skill_dir / "SKILL.md"
    try:
        resolved = str(skill_file.resolve())
        st = skill_file.stat()
    except OSError:
        return skill_dir.name, "", None

    cache = _load_metadata_cache()
    entry = cache.get(resolved)
    if (
        isinstance(entry, dict)
        and entry.get("mtime_ns") == st.st_mtime_ns
        and entry.get("size") == st.st_size
    ):
        return (
            entry.get("name", skill_dir.name),
            entry.get("description", ""),
            entry.get("updated_at", st.st_mtime),
        )

    name, description, frontmatter_updated = parse_skill_meta(skill_dir)
    updated_at = frontmatter_updated or st.st_mtime
    cache[resolved] = {
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "name": name,
        "description": description,
        "updated_at": updated_at,
    }
    _METADATA_CACHE_DIRTY = True
    return name, description, updated_at


def format_global_location(path_or_label: str) -> str:
    """Format global path as a short 'G: <agent>' label for picker and search."""
    cleaned = path_or_label.removeprefix("global:")
    parts = Path(cleaned).parts
    if len(parts) >= 2 and parts[-1] == "skills":
        agent_part = parts[-2]
    else:
        agent_part = parts[-1] if parts else cleaned
    agent_name = agent_part.lstrip(".")
    return f"G: {agent_name}" if agent_name else "G: global"


def rows_for(preset: str, skills: dict, scope: str = "preset") -> list:
    """Pickable rows for one location's skills, in name order."""
    rows = []
    for folder, path in sorted(skills.items()):
        name, description, updated_at = skill_meta(path)
        rows.append({
            "skill": folder,
            "preset": preset,
            "scope": scope,
            "location": preset if scope == "preset" else format_global_location(preset),
            "name": name,
            "description": description,
            "updated_at": updated_at,
            "path": str(path),
        })
    _save_metadata_cache()
    return rows


def wants_picker(config: dict, explicit: bool = False) -> bool:
    """Whether to ask with fzf rather than a numbered list.

    `prompts.picker`: "auto" (the default) uses fzf when it is installed and there
    is a terminal for it to draw on, "fzf" always tries, "numbers" never does.
    Asking for it on the command line overrides the setting.
    """
    if explicit:
        return True
    mode = str(config.get("prompts", {}).get("picker", "auto")).lower()
    if mode == "fzf":
        return True
    if mode in ("numbers", "number", "none", "false"):
        return False
    return bool(shutil.which("fzf")) and sys.stdin.isatty()


def pick(rows: list, header: Optional[str] = None, action: str = "apply", query: str = "") -> list:
    """Choose from the rows: fzf when it can run, a numbered list otherwise."""
    if shutil.which("fzf"):
        chosen, ran = pick_with_fzf(rows, header, query)
        if ran:
            return chosen
        print_warn("fzf could not run here; falling back to a numbered list.")

    labels = [f"{row.get('location', row['preset'])}/{row['skill']}" for row in rows]
    chosen_labels = set(prompt_skills(labels, action=action))
    return [row for row, label in zip(rows, labels) if label in chosen_labels]


def pick_with_fzf(rows: list, header: Optional[str] = None, query: str = "") -> tuple:
    """Hand the rows to fzf, with each skill's SKILL.md in the preview pane.

    Returns what was chosen and whether fzf ran at all. fzf draws on stderr and
    reads the keyboard from /dev/tty, not from the stdin we feed the candidates
    through, so only stdout is captured. Choosing nothing exits 1 or 130 and is no
    error; only its own failure (exit 2, no terminal to draw on, say) sends the
    choice back to the numbered prompt.
    """
    colors = picker_ansi(load_config().get("theme"))
    reset = colors["reset"]
    dim = colors.get("updated", colors.get("description", "\x1b[2m"))
    lines = [
        "\t".join((
            f"{colors['name']}{truncate(row['skill'], PICK_NAME_WIDTH):<{PICK_NAME_WIDTH}}{reset}",
            f"{colors['location']}{truncate(row.get('location') or row.get('preset', ''), PICK_LOCATION_WIDTH):<{PICK_LOCATION_WIDTH}}{reset}",
            f"{dim}{truncate(format_relative_time(row.get('updated_at'), short=True) or '-', PICK_UPDATED_WIDTH):<{PICK_UPDATED_WIDTH}}{reset}",
            f"{colors['description']}{truncate(row['description'], PICK_DESC_WIDTH):<{PICK_DESC_WIDTH}}{reset}",
            row["path"],
        ))
        for row in rows
    ]
    result = subprocess.run(
        [
            "fzf", "--multi", "--ansi", "--tabstop", "2", "--query", query,
            "--delimiter", "\t", "--with-nth", "1,2,3,4",
            "--header", header or DEFAULT_HEADER,
            "--color", fzf_color_arg(load_config().get("theme")),
            "--expect", "alt-p,alt-g",
            "--bind", "ctrl-a:select-all,ctrl-d:deselect-all",
            "--preview", preview_command(),
            "--preview-window", "right,55%,wrap",
        ],
        input="\n".join(lines), stdout=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode >= 2 and result.returncode != 130:
        return [], False
    output_lines = result.stdout.splitlines()
    action = output_lines.pop(0) if output_lines and output_lines[0] in ("alt-p", "alt-g") else None
    if output_lines and not output_lines[0]:
        output_lines.pop(0)
    picked = {
        line.split("\t")[4] for line in output_lines
        if line.strip() and len(line.split("\t")) >= 5
    }
    chosen = []
    for row in rows:
        if row["path"] in picked:
            row_copy = dict(row)
            row_copy["_action"] = action
            chosen.append(row_copy)
    return chosen, True
