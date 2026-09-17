"""Search skills.sh and inspect a chosen skill before installation."""

import json
import hashlib
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml
from rich.prompt import Confirm, Prompt
from rich.table import Table

from skill_ctl.config import load_config
from skill_ctl.theme import bat_theme, fzf_color_arg, picker_ansi
from skill_ctl.ui import console, print_error, print_header, print_warn

# `npx skills` reads the same override, so pointing both at one mirror takes a
# single variable. It also lets the search be aimed at a local stub in testing.
SKILLS_URL = os.environ.get("SKILLS_API_URL") or "https://skills.sh"
GITHUB_API = "https://api.github.com"
SEARCH_LIMIT = 50
# skills.sh has answered this search anywhere between 0.8s and 10.8s, so a 10s
# ceiling cut off replies that were still on their way and reported them as a
# connection failure. Waiting is the better failure here: the debounce means
# one request is in flight at a time, and fzf keeps showing the previous
# results until this one lands.
SEARCH_TIMEOUT = 20
# Below this, every extra keystroke would still fire a live search request;
# a real query is rarely 1-2 characters anyway, so this cuts a lot of wasted
# skills.sh calls while typing.
MIN_QUERY_LENGTH = 3
# Seconds a keystroke waits before its search leaves for skills.sh. Long
# enough to swallow ordinary typing, short enough to feel immediate once the
# typing stops.
SEARCH_DEBOUNCE = "0.25"
NAME_WIDTH = 22
SOURCE_WIDTH = 28
INSTALLS_WIDTH = 11

# Cached per process: write_rows runs as a fresh `python -m skill_ctl.catalog
# --rows` subprocess on every fzf keystroke, so one config read per process is
# already the natural cadence - no need to re-read it per row.
_colors = None


def shell_quote(value: str) -> str:
    """Quote a token for the shell fzf runs its reload/preview commands in.

    fzf on Windows hands these to cmd.exe, which has no concept of the single
    quotes shlex.quote() produces - it treats them as literal characters, so a
    quoted path stops being a valid path. cmd.exe's own quoting is double
    quotes instead.
    """
    if os.name == "nt":
        return '"' + value.replace('"', '""') + '"'
    return shlex.quote(value)


def row_colors() -> dict[str, str]:
    global _colors
    if _colors is None:
        _colors = picker_ansi(load_config().get("theme"))
    return _colors


def get_json(url: str, timeout: float = 10) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "skill-ctl"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def search(query: str, owner: Optional[str] = None) -> list[dict]:
    params = {"q": query, "limit": str(SEARCH_LIMIT)}
    if owner:
        params["owner"] = owner
    try:
        data = get_json(f"{SKILLS_URL}/api/search?{urlencode(params)}", timeout=SEARCH_TIMEOUT)
    except HTTPError as error:
        if error.code == 429:
            print_error("skills.sh is rate limiting this search. Wait a moment and try again.")
        else:
            print_error(f"Could not search skills.sh: {error}")
        return []
    except TimeoutError:
        print_error(f"skills.sh did not answer within {SEARCH_TIMEOUT}s. Try again in a moment.")
        return []
    except (OSError, URLError, ValueError) as error:
        print_error(f"Could not search skills.sh: {error}")
        return []
    return sorted(data.get("skills", []), key=lambda item: item.get("installs") or 0, reverse=True)


def format_installs(value: object) -> str:
    count = int(value or 0)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(count) if count else "-"


def truncate(value: object, width: int) -> str:
    """Fit a value in an fzf column without letting it push other columns away."""
    text = str(value or "")
    return text if len(text) <= width else text[:width - 1] + "…"


def format_row(skill: dict) -> str:
    """Render the visible, fixed-width part of a live fzf result."""
    name = truncate(skill.get("name"), NAME_WIDTH)
    source = truncate(skill.get("source") or skill.get("id"), SOURCE_WIDTH)
    installs = format_installs(skill.get("installs"))
    colors = row_colors()
    reset = colors["reset"]
    return (
        f"{colors['name']}{name:<{NAME_WIDTH}}{reset} "
        f"{colors['description']}{installs:<{INSTALLS_WIDTH}}{reset} "
        f"{colors['location']}{source:<{SOURCE_WIDTH}}{reset}"
    )


def choose(query: str = "", owner: Optional[str] = None) -> list[dict]:
    if shutil.which("fzf") and sys.stdin.isatty():
        return choose_with_fzf(query, owner)
    if not query:
        try:
            query = Prompt.ask("Search skills", console=console).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return []
    results = search(query, owner)
    if not results:
        print_warn("No skills found.")
        return []
    table = Table(title=f"skills.sh results for '{query}'", header_style="header")
    table.add_column("#", justify="right")
    table.add_column("Skill", style="bold")
    table.add_column("Installs", justify="right")
    table.add_column("Source")
    for index, skill in enumerate(results, 1):
        table.add_row(str(index), str(skill.get("name", "")), format_installs(skill.get("installs")), str(skill.get("source") or skill.get("id", "")))
    console.print(table)
    try:
        selected = Prompt.ask(
            "Skill number(s) to inspect [dim](space/comma separated for several)[/dim], or press Enter to cancel",
            default="", console=console,
        ).strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return []
    if not selected:
        return []
    chosen = []
    for token in selected.replace(",", " ").split():
        if token.isdigit() and 1 <= int(token) <= len(results):
            chosen.append(results[int(token) - 1])
        else:
            print_warn(f"Ignoring invalid selection '{token}'")
    if not chosen:
        print_warn("No matching skill selected.")
    return chosen


def cached_search(query: str, cache_dir: Path, owner: Optional[str] = None) -> list[dict]:
    """search(), remembering each query for the lifetime of one picker session.

    Backspacing to a query already typed, or retyping one, is common enough
    that re-asking skills.sh for it wastes most of a second. Only a non-empty
    result is stored, so a failed or rate-limited call is retried rather than
    remembered as "no matches".
    """
    key = hashlib.sha256(f"{query}\0{owner or ''}".encode()).hexdigest()
    path = cache_dir / f"query-{key}.json"
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    skills = search(query, owner)
    if skills:
        try:
            path.write_text(json.dumps(skills), encoding="utf-8")
        except OSError:
            pass
    return skills


def write_rows(query: str, cache_dir: Path, owner: Optional[str] = None) -> list[str]:
    """Write fzf candidates and their preview inputs for one skills.sh query."""
    if len(query.strip()) < MIN_QUERY_LENGTH:
        return []
    rows = []
    for skill in cached_search(query.strip(), cache_dir, owner):
        key = f"{skill.get('source') or skill.get('id', '')}:{skill.get('name', '')}"
        path = cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
        path.write_text(json.dumps(skill), encoding="utf-8")
        rows.append(f"{format_row(skill)}\t{path}")
    return rows


def choose_with_fzf(query: str, owner: Optional[str] = None) -> list[dict]:
    """Browse a live skills.sh search with details loaded for the highlighted row."""
    with tempfile.TemporaryDirectory(prefix="skctl-find-") as temporary:
        root = Path(temporary)
        preview = preview_command()
        rows = f"{shell_quote(sys.executable)} -m skill_ctl.catalog --rows --cache-dir {shell_quote(str(root))} --query {{q}}"
        if owner:
            rows += f" --owner {shell_quote(owner)}"
        # fzf kills a running reload when the next one starts, so a delay at
        # the front of the reload debounces typing: only the keystroke you stop
        # on survives long enough to reach skills.sh. Without it every
        # character fired its own request, which made results trail the typing
        # by most of a second and could trip the API's rate limit mid-word.
        # The delay is done by the subprocess itself, not a shell `sleep`: on
        # Windows fzf hands reload commands to cmd.exe, which has no sleep and
        # no `;`. The opening query is already known, so `start` runs undelayed.
        debounced_rows = f"{rows} --debounce {SEARCH_DEBOUNCE}"
        result = subprocess.run(
            [
                "fzf", "--multi", "--ansi", "--phony", "--disabled", "--query", query,
                "--delimiter", "\t", "--with-nth", "1",
                "--header", (
                    f"{'Skill':<{NAME_WIDTH}} {'Installs':<{INSTALLS_WIDTH}} Source\n"
                    "Tab · ctrl-a all · ctrl-d none · Alt-A repo · Alt-C query"
                ),
                "--color", fzf_color_arg(load_config().get("theme")),
                "--expect", "alt-a",
                "--bind", "ctrl-a:select-all,ctrl-d:deselect-all",
                "--bind", f"alt-c:transform-query({shell_quote(sys.executable)} -m skill_ctl.catalog --source-file {{2}})",
                "--bind", f"start:reload({rows})+refresh-preview",
                "--bind", f"change:reload({debounced_rows})+refresh-preview",
                "--preview", preview, "--preview-window", "right,55%,wrap",
            ],
            stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        selected = result.stdout.splitlines()
        action = selected.pop(0) if selected and selected[0] == "alt-a" else None
        if selected and not selected[0]:
            selected.pop(0)
        chosen = []
        for line in selected:
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            try:
                skill = json.loads(Path(fields[1]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            skill["_details"] = read_preview(Path(fields[1]))
            chosen.append(skill)
        if action == "alt-a" and chosen:
            chosen = [{"name": "all skills", "source": chosen[0].get("source") or chosen[0].get("id"), "_all_from_source": True}]
        return chosen


def preview_command() -> str:
    """Render catalog metadata and SKILL.md through bat when it is available."""
    command = f"{shell_quote(sys.executable)} -m skill_ctl.catalog --preview-line {{}}"
    renderer = "bat" if shutil.which("bat") else "batcat" if shutil.which("batcat") else None
    if not renderer:
        return command
    theme = shell_quote(bat_theme(load_config().get("theme")))
    # cmd.exe expands environment variables as %VAR%, not $VAR - the latter
    # reaches bat unexpanded and it rejects the literal string as a width.
    width_var = "%FZF_PREVIEW_COLUMNS%" if os.name == "nt" else '"$FZF_PREVIEW_COLUMNS"'
    return (
        f"{command} | {renderer} --color=always --paging=never --style=plain "
        f"--language=md --theme={theme} --squeeze-blank --wrap=character "
        f"--terminal-width={width_var}"
    )


def github_source(source: str) -> Optional[str]:
    parts = source.removesuffix(".git").split("/")
    if len(parts) == 2 and all(parts):
        return "/".join(parts)
    return None


def _fetch_raw_github_file(repo: str, path: str, timeout: float = 4.0) -> Optional[str]:
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
    req = Request(url, headers={"User-Agent": "skill-ctl"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def frontmatter(source: str, skill_name: str) -> dict:
    repo = github_source(source)
    if not repo:
        return {}

    # Fast path: try standard locations directly from raw CDN before hitting
    # the GitHub recursive trees API (which has strict 60 req/hr rate limits).
    path = None
    text = None
    for candidate in (f"{skill_name}/SKILL.md", f"skills/{skill_name}/SKILL.md", "SKILL.md"):
        content = _fetch_raw_github_file(repo, candidate)
        if content is not None:
            path = candidate
            text = content
            break

    if text is None:
        try:
            tree = get_json(f"{GITHUB_API}/repos/{repo}/git/trees/HEAD?recursive=1").get("tree", [])
            candidates = [entry["path"] for entry in tree if entry.get("type") == "blob" and (entry.get("path") == "SKILL.md" or entry.get("path", "").endswith("/SKILL.md"))]
            exact = [p for p in candidates if p != "SKILL.md" and p.rsplit("/", 2)[-2].lower() == skill_name.lower()]
            path = (exact or candidates)[0]
            request = Request(f"https://raw.githubusercontent.com/{repo}/HEAD/{path}", headers={"User-Agent": "skill-ctl"})
            with urlopen(request, timeout=10) as response:
                text = response.read().decode("utf-8", errors="replace")
        except (IndexError, KeyError, OSError, URLError, ValueError):
            return {}

    if not text.startswith("---"):
        return {"path": path, "content": text}
    _, _, remaining = text.partition("\n")
    block, separator, _ = remaining.partition("\n---")
    if not separator:
        return {"path": path, "content": text}
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        parsed = {}
    return {"path": path, "content": text, **(parsed if isinstance(parsed, dict) else {})}


def details(skill: dict) -> dict:
    """Collect the small amount of information worth reading before install."""
    name = str(skill.get("name", ""))
    source = str(skill.get("source") or skill.get("id", ""))
    repo = github_source(source)
    stars = None
    if repo:
        try:
            stars = get_json(f"{GITHUB_API}/repos/{repo}").get("stargazers_count")
        except (OSError, URLError, ValueError):
            pass
    return {
        "name": name,
        "source": source,
        "installs": format_installs(skill.get("installs")),
        "stars": stars,
        "metadata": frontmatter(source, name),
        "url": f"{SKILLS_URL}/{skill.get('id', '')}",
    }


def detail_lines(data: dict) -> list[str]:
    metadata = data.get("metadata", {})
    lines = [
        data.get("name", ""),
        "",
        f"Source: {data.get('source', '')}",
        f"Installs: {data.get('installs', '-')}",
    ]
    if data.get("stars") is not None:
        lines.append(f"GitHub stars: {data['stars']:,}")
    if metadata.get("description"):
        lines.extend(("", "Description:", " ".join(str(metadata["description"]).split())))
    if metadata.get("path"):
        lines.append(f"SKILL.md: {metadata['path']}")
    lines.extend(("", f"Details: {data.get('url', '')}"))
    return lines


def preview_lines(data: dict) -> list[str]:
    """Include the fetched SKILL.md in fzf's preview, but not in the confirmation."""
    lines = detail_lines(data)
    content = str(data.get("metadata", {}).get("content") or "").strip()
    if content:
        lines.extend(("", "--- SKILL.md ---", content))
    return lines


def read_preview(path: Path) -> dict:
    cache = path.with_suffix(".details.json")
    try:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        skill = json.loads(path.read_text(encoding="utf-8"))
        data = details(skill)
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data
    except (OSError, ValueError):
        return {}


def inspect(skills: list[dict]) -> bool:
    """Show what was picked, then ask once whether to install it.

    A single pick gets the full preview (stars, description, SKILL.md path);
    several picks get a compact list instead - fetching stars for each would
    mean one GitHub API call per skill, worth paying for one skill but not
    worth it (or the wait) for a batch.
    """
    if not skills:
        return False
    if len(skills) == 1:
        skill = skills[0]
        if skill.get("_all_from_source"):
            source = str(skill.get("source") or skill.get("id", ""))
            print_header(f"Choose skills from {source}")
            console.print("  [dim]npx skills will open its own picker.[/dim]")
            return Confirm.ask("Continue to installation?", default=True, console=console)
        data = skill.get("_details") or details(skill)
        print_header(str(data.get("name", skill.get("name", ""))))
        for line in detail_lines(data)[2:]:
            console.print(f"  {line}" if line else "")
        prompt = "Continue to installation?"
    else:
        console.print()
        for skill in skills:
            name = str(skill.get("name", ""))
            source = str(skill.get("source") or skill.get("id", ""))
            installs = format_installs(skill.get("installs"))
            console.print(f"  [header]{name}[/header] [dim]({source}, {installs} installs)[/dim]")
        prompt = f"\nInstall {len(skills)} skills?"
    try:
        return Confirm.ask(prompt, default=True, console=console)
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False


if __name__ == "__main__":
    # This runs as fzf's reload/preview subprocess, writing to a pipe rather
    # than a real console - Windows then picks the ANSI codepage (cp1252)
    # instead of UTF-8, and skill text outside that codepage crashes the print.
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) == 3 and sys.argv[1] == "--preview-file":
        print("\n".join(detail_lines(read_preview(Path(sys.argv[2])))))
    elif len(sys.argv) == 3 and sys.argv[1] == "--preview-line":
        fields = sys.argv[2].split("\t")
        if len(fields) < 2:
            print(f"Type at least {MIN_QUERY_LENGTH} characters to search skills.sh.")
        else:
            print("\n".join(preview_lines(read_preview(Path(fields[-1])))))
    elif len(sys.argv) == 3 and sys.argv[1] == "--source-file":
        try:
            skill = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
            print(skill.get("source") or skill.get("id", ""))
        except (OSError, ValueError):
            pass
    elif len(sys.argv) >= 2 and sys.argv[1] == "--rows":
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--rows", action="store_true")
        parser.add_argument("--cache-dir", required=True)
        parser.add_argument("--query", required=True)
        parser.add_argument("--owner")
        parser.add_argument("--debounce", type=float, default=0.0)
        args = parser.parse_args()
        if args.debounce > 0:
            # Typing again makes fzf kill this process; sleeping first means a
            # keystroke that gets superseded never costs a skills.sh request.
            import time

            time.sleep(args.debounce)
        print("\n".join(write_rows(args.query, Path(args.cache_dir), args.owner)))
    else:
        raise SystemExit("catalog preview is only available through `skctl find`")
