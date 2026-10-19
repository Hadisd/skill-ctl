"""Fast native filesystem scanner for installed agent skills across project and global paths."""

import json
import os
from pathlib import Path
from typing import Optional

from skill_ctl.core.constants import PRESETS_DIR

AGENT_NAME_EXPANSIONS = {
    ".agents/skills": [
        "Amp",
        "Antigravity",
        "Antigravity CLI",
        "Claude Code",
        "Codex",
        "Cursor",
        "Gemini CLI",
        "GitHub Copilot",
        "OpenCode",
        "Zed",
    ],
    ".claude/skills": ["Claude Code"],
    ".cursor/skills": ["Cursor"],
    ".codex/skills": ["Codex"],
    ".codeium/windsurf/skills": ["Windsurf"],
    ".windsurf/skills": ["Windsurf"],
    ".hermes/skills": ["Hermes"],
    ".github/skills": ["GitHub Copilot"],
}

CHECK_DIRS = [
    (".agents/skills", ".agents/skills"),
    (".claude/skills", ".claude/skills"),
    (".cursor/skills", ".cursor/skills"),
    (".codex/skills", ".codex/skills"),
    (".codeium/windsurf/skills", ".codeium/windsurf/skills"),
    (".windsurf/skills", ".windsurf/skills"),
    (".hermes/skills", ".hermes/skills"),
    (".github/skills", ".github/skills"),
]


def scan_installed_skills(
    target_root: Path,
    is_global: bool,
    agent_filter: Optional[str] = None,
) -> list[dict]:
    """Fast native filesystem scanner for installed agent skills."""
    skills_json_path = target_root / ".skills.json"
    skills_meta = {}
    if skills_json_path.is_file():
        try:
            raw = json.loads(skills_json_path.read_text(encoding="utf-8"))
            skills_meta = raw.get("skills", {})
        except Exception:
            pass

    found_skills: dict[str, dict] = {}

    for rel_dir, _ in CHECK_DIRS:
        dir_path = target_root / rel_dir
        if not dir_path.is_dir():
            continue
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if not (entry.is_dir() or entry.is_symlink()):
                        continue
                    skill_name = entry.name
                    if skill_name not in found_skills:
                        meta = skills_meta.get(skill_name, {})
                        source = meta.get("source")
                        source_url = meta.get("sourceUrl")
                        source_type = meta.get("sourceType")
                        if not source:
                            try:
                                resolved = Path(entry.path).resolve()
                                if PRESETS_DIR.resolve() in resolved.parents:
                                    pname = resolved.relative_to(PRESETS_DIR.resolve()).parts[0]
                                    source = f"preset ({pname})"
                                else:
                                    source = "local"
                            except Exception:
                                source = "local"
                        found_skills[skill_name] = {
                            "name": skill_name,
                            "path": str(entry.path),
                            "scope": "global" if is_global else "project",
                            "agents": set(),
                            "source": source,
                            "sourceUrl": source_url,
                            "sourceType": source_type,
                        }
                    agents_for_dir = AGENT_NAME_EXPANSIONS.get(rel_dir, [rel_dir])
                    found_skills[skill_name]["agents"].update(agents_for_dir)
        except OSError:
            pass

    results = []
    for name, data in sorted(found_skills.items()):
        agent_list = sorted(data["agents"])
        if agent_filter:
            filter_lower = agent_filter.lower()
            matching = [a for a in agent_list if filter_lower in a.lower()]
            if not matching:
                agents_display = ["not linked"]
            else:
                agents_display = matching
        else:
            agents_display = agent_list

        results.append(
            {
                "name": name,
                "path": data["path"],
                "scope": data["scope"],
                "agents": agents_display,
                "source": data["source"],
                "sourceUrl": data["sourceUrl"],
                "sourceType": data["sourceType"],
            }
        )
    return results


def _format_agents_summary(agents: list[str]) -> str:
    if not agents or agents == ["not linked"]:
        return "not linked"
    if len(agents) > 5:
        return f"{', '.join(agents[:5])} +{len(agents) - 5} more"
    return ", ".join(agents)


def _display_path(p: Path | str) -> str:
    path_obj = Path(p)
    try:
        if path_obj.is_relative_to(Path.home()):
            return "~/" + path_obj.relative_to(Path.home()).as_posix()
        return str(path_obj)
    except Exception:
        return str(path_obj)
