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
PICK_DESC_WIDTH = 42

# Keys worth knowing, spelled out rather than left to be discovered.
DEFAULT_HEADER = (
    "Skill                   Location      Description\n"
    "Tab · ctrl-a all · ctrl-d none · Enter apply"
)


def truncate(value: object, width: int) -> str:
    text = str(value or "").replace("\t", " ")
    return text if len(text) <= width else text[:width - 1] + "…"


def preview_command() -> str:
    """Use an installed Markdown renderer, with plain text as the fallback."""
    skill_file = r"{4}\SKILL.md" if os.name == "nt" else "{4}/SKILL.md"
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


def parse_skill_meta(skill_dir: Path) -> tuple[str, str]:
    """Parse name and description from a skill's SKILL.md frontmatter."""
    name, description = skill_dir.name, ""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return name, description
    if not text.startswith("---"):
        return name, description

    _, _, rest = text.partition("\n")
    block, sep, _ = rest.partition("\n---")
    if not sep:
        return name, description

    # Search reads this metadata for every installed skill. PyYAML is much more
    # work than the two scalar fields need, especially across large presets.
    values = {}
    needs_yaml = False
    lines = block.splitlines()
    for index, line in enumerate(lines):
        key, marker, value = line.partition(":")
        if not marker or key not in ("name", "description"):
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
            return name, description
        if not isinstance(values, dict):
            return name, description

    name = values.get("name") or name
    description = " ".join(values.get("description", "").split())
    return name, description


def skill_meta(skill_dir: Path) -> tuple:
    """The name and description from a skill's SKILL.md frontmatter.

    Falls back to the directory name and no description: a skill without readable
    frontmatter is still a skill, and must stay findable by name.
    Uses an mtime/size-validated cache for fast repeat searches.
    """
    global _METADATA_CACHE_DIRTY
    skill_file = skill_dir / "SKILL.md"
    try:
        resolved = str(skill_file.resolve())
        st = skill_file.stat()
    except OSError:
        return skill_dir.name, ""

    cache = _load_metadata_cache()
    entry = cache.get(resolved)
    if (
        isinstance(entry, dict)
        and entry.get("mtime_ns") == st.st_mtime_ns
        and entry.get("size") == st.st_size
    ):
        return entry.get("name", skill_dir.name), entry.get("description", "")

    name, description = parse_skill_meta(skill_dir)
    cache[resolved] = {
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "name": name,
        "description": description,
    }
    _METADATA_CACHE_DIRTY = True
    return name, description


def rows_for(preset: str, skills: dict, scope: str = "preset") -> list:
    """Pickable rows for one location's skills, in name order."""
    rows = []
    for folder, path in sorted(skills.items()):
        name, description = skill_meta(path)
        rows.append({
            "skill": folder,
            "preset": preset,
            "scope": scope,
            "location": preset if scope == "preset" else f"global:{preset}",
            "name": name,
            "description": description,
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
    lines = [
        "\t".join((
            f"{colors['name']}{truncate(row['skill'], PICK_NAME_WIDTH):<{PICK_NAME_WIDTH}}{reset}",
            f"{colors['location']}{truncate(row.get('location', row['preset']), PICK_LOCATION_WIDTH):<{PICK_LOCATION_WIDTH}}{reset}",
            f"{colors['description']}{truncate(row['description'], PICK_DESC_WIDTH):<{PICK_DESC_WIDTH}}{reset}",
            row["path"],
        ))
        for row in rows
    ]
    result = subprocess.run(
        [
            "fzf", "--multi", "--ansi", "--tabstop", "2", "--query", query,
            "--delimiter", "\t", "--with-nth", "1,2,3",
            "--header", header or DEFAULT_HEADER,
            "--color", fzf_color_arg(load_config().get("theme")),
            # ctrl-a takes everything that is showing, so narrowing by typing and
            # then taking the lot is two keys; ctrl-d undoes it.
            "--bind", "ctrl-a:select-all,ctrl-d:deselect-all",
            "--preview", preview_command(),
            "--preview-window", "right,55%,wrap",
        ],
        input="\n".join(lines), stdout=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode >= 2 and result.returncode != 130:
        return [], False
    picked = {
        line.split("\t")[3] for line in result.stdout.splitlines()
        if line.strip() and len(line.split("\t")) >= 4
    }
    return [row for row in rows if row["path"] in picked], True
