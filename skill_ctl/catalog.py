"""Search skills.sh, skillsmp.com, and other remote registries."""

import json
import hashlib
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml
from rich.prompt import Confirm, Prompt
from rich.table import Table

from skill_ctl.config import load_config, get_search_remotes
from skill_ctl.theme import bat_theme, fzf_color_arg, picker_ansi
from skill_ctl.ui import console, print_error, print_header, print_warn

# `npx skills` reads the same override, so pointing both at one mirror takes a
# single variable. It also lets the search be aimed at a local stub in testing.
SKILLS_URL = os.environ.get("SKILLS_API_URL") or "https://skills.sh"
SKILLSMP_URL = os.environ.get("SKILLSMP_API_URL") or "https://skillsmp.com"
GITHUB_API = "https://api.github.com"
SEARCH_LIMIT = 50
SEARCH_TIMEOUT = 20
MIN_QUERY_LENGTH = 3
SEARCH_DEBOUNCE = "0.25"
NAME_WIDTH = 22
METRIC_WIDTH = 10
UPDATED_WIDTH = 10
SOURCE_WIDTH = 26
REMOTE_WIDTH = 10

# Cached per process: write_rows runs as a fresh `python -m skill_ctl.catalog
# --rows` subprocess on every fzf keystroke, so one config read per process is
# already the natural cadence - no need to re-read it per row.
_colors = None


def shell_quote(value: str) -> str:
    """Quote a token for the shell fzf runs its reload/preview commands in."""
    if os.name == "nt":
        return '"' + value.replace('"', '""') + '"'
    return shlex.quote(value)


def row_colors() -> dict[str, str]:
    global _colors
    if _colors is None:
        _colors = picker_ansi(load_config().get("theme"))
    return _colors


def get_json(url: str, timeout: float = 10) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "skill-ctl"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def parse_github_repo(url_or_str: str) -> Optional[str]:
    """Extract owner/repo from GitHub URLs or raw owner/repo shorthand."""
    if not url_or_str:
        return None
    val = str(url_or_str).strip()
    if val.startswith("git@github.com:"):
        val = val.removeprefix("git@github.com:").removesuffix(".git")
        return val
    try:
        parsed = urllib.parse.urlparse(val)
        if parsed.netloc and "github.com" in parsed.netloc:
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}".removesuffix(".git")
    except Exception:
        pass
    parts = val.removesuffix(".git").split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return val.removesuffix(".git")
    return None


def search_skills_sh(query: str, owner: Optional[str] = None, url: str = SKILLS_URL, timeout: float = SEARCH_TIMEOUT) -> list[dict]:
    """Search skills.sh API for agent skills."""
    if not query or len(query.strip()) < MIN_QUERY_LENGTH:
        return []
    params = {"q": query.strip(), "limit": str(SEARCH_LIMIT)}
    if owner:
        params["owner"] = owner
    try:
        data = get_json(f"{url.rstrip('/')}/api/search?{urlencode(params)}", timeout=timeout)
    except HTTPError as error:
        if error.code == 429:
            print_error("skills.sh is rate limiting this search. Wait a moment and try again.")
        return []
    except Exception:
        return []

    results = []
    for item in data.get("skills", []):
        results.append({
            "name": item.get("name", ""),
            "source": item.get("source") or item.get("id", ""),
            "id": item.get("id", ""),
            "installs": item.get("installs"),
            "stars": None,
            "updated_at": item.get("updated_at") or item.get("updatedAt"),
            "description": "",
            "remote": "skills.sh",
            "url": f"{url.rstrip('/')}/{item.get('id', '')}",
        })
    return results


def search_skillsmp(query: str, owner: Optional[str] = None, url: str = SKILLSMP_URL, timeout: float = SEARCH_TIMEOUT) -> list[dict]:
    """Search skillsmp.com API for agent skills."""
    if not query or len(query.strip()) < MIN_QUERY_LENGTH:
        return []
    params = {"q": query.strip(), "limit": str(SEARCH_LIMIT)}
    try:
        data = get_json(f"{url.rstrip('/')}/api/v1/skills/search?{urlencode(params)}", timeout=timeout)
    except HTTPError as error:
        if error.code == 429:
            print_error("skillsmp.com is rate limiting this search. Wait a moment and try again.")
        return []
    except Exception:
        return []

    skills_list = data.get("data", {}).get("skills", []) if isinstance(data, dict) else []
    results = []
    for item in skills_list:
        gh_url = item.get("githubUrl", "")
        repo_source = parse_github_repo(gh_url) or (f"{item.get('author')}/{item.get('name')}" if item.get("author") else item.get("name", ""))
        if owner:
            owner_lower = owner.lower()
            author_lower = str(item.get("author", "")).lower()
            source_lower = repo_source.lower()
            if not (source_lower.startswith(f"{owner_lower}/") or author_lower == owner_lower):
                continue
        results.append({
            "name": item.get("name", ""),
            "source": repo_source,
            "id": item.get("id", ""),
            "installs": None,
            "stars": item.get("stars"),
            "updated_at": item.get("updatedAt"),
            "description": item.get("description", ""),
            "remote": "skillsmp",
            "url": item.get("skillUrl") or gh_url or f"{url.rstrip('/')}/skills/{item.get('id', '')}",
            "github_url": gh_url,
        })
    return results


def search(query: str, owner: Optional[str] = None, remotes: Optional[list[str]] = None) -> list[dict]:
    """Search across all configured or requested remote catalogs concurrently."""
    if not query or len(query.strip()) < MIN_QUERY_LENGTH:
        return []

    if remotes:
        selected_names = {r.strip().lower() for item in remotes for r in item.split(",") if r.strip()}
        if "all" in selected_names or "*" in selected_names:
            all_remotes = [
                {"name": "skills.sh", "url": SKILLS_URL, "type": "skills_sh", "enabled": True},
                {"name": "skillsmp", "url": SKILLSMP_URL, "type": "skillsmp", "enabled": True},
            ]
        else:
            filtered = [
                r for r in get_search_remotes()
                if r.get("name", "").lower() in selected_names or r.get("type", "").lower() in selected_names
            ]
            if filtered:
                all_remotes = filtered
            else:
                all_remotes = []
                for name in selected_names:
                    if "skillsmp" in name:
                        all_remotes.append({"name": "skillsmp", "url": SKILLSMP_URL, "type": "skillsmp", "enabled": True})
                    elif "skills.sh" in name or "skills" in name:
                        all_remotes.append({"name": "skills.sh", "url": SKILLS_URL, "type": "skills_sh", "enabled": True})
    else:
        all_remotes = get_search_remotes()

    results_by_key: dict[tuple[str, str], dict] = {}

    def run_provider(remote_info: dict) -> list[dict]:
        r_type = remote_info.get("type", remote_info.get("name", "")).lower()
        r_url = remote_info.get("url", "")
        if "skillsmp" in r_type or "skillsmp" in r_url:
            return search_skillsmp(query, owner=owner, url=r_url or SKILLSMP_URL)
        return search_skills_sh(query, owner=owner, url=r_url or SKILLS_URL)

    with ThreadPoolExecutor(max_workers=max(len(all_remotes), 1)) as pool:
        futures = {pool.submit(run_provider, r): r for r in all_remotes}
        for fut in as_completed(futures):
            try:
                items = fut.result()
            except Exception:
                items = []
            for item in items:
                key = (item.get("source", "").lower(), item.get("name", "").lower())
                if key not in results_by_key:
                    results_by_key[key] = dict(item)
                else:
                    existing = results_by_key[key]
                    if not existing.get("installs") and item.get("installs"):
                        existing["installs"] = item["installs"]
                    if not existing.get("stars") and item.get("stars"):
                        existing["stars"] = item["stars"]
                    if not existing.get("updated_at") and item.get("updated_at"):
                        existing["updated_at"] = item["updated_at"]
                    if not existing.get("description") and item.get("description"):
                        existing["description"] = item["description"]
                    r1 = existing.get("remote", "")
                    r2 = item.get("remote", "")
                    if r2 and r2 not in r1:
                        existing["remote"] = f"{r1}, {r2}"

    def sort_key(skill: dict):
        stars = skill.get("stars") or 0
        installs = skill.get("installs") or 0
        return (max(stars, installs), installs, stars)

    return sorted(results_by_key.values(), key=sort_key, reverse=True)


def format_number(value: object) -> str:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(count) if count else "-"


def format_installs(value: object) -> str:
    return format_number(value)


def format_metric(installs: object, stars: object) -> str:
    try:
        s_count = int(stars or 0)
    except (TypeError, ValueError):
        s_count = 0
    try:
        i_count = int(installs or 0)
    except (TypeError, ValueError):
        i_count = 0

    if s_count > 0:
        return f"★ {format_number(s_count)}"
    if i_count > 0:
        return f"↓ {format_number(i_count)}"
    return "-"


def format_relative_time(value: object, short: bool = False) -> Optional[str]:
    """Format an ISO timestamp string or Unix timestamp into a relative human-readable string."""
    if not value:
        return None
    dt = None
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 1e11:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    elif isinstance(value, str):
        val = value.strip()
        if not val:
            return None
        if val.isdigit():
            return format_relative_time(int(val), short=short)
        try:
            if "." in val and val.replace(".", "", 1).isdigit():
                return format_relative_time(float(val), short=short)
        except Exception:
            pass
        try:
            val = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    if not dt:
        return None

    now = datetime.now(timezone.utc)
    diff = now - dt
    total_seconds = int(diff.total_seconds())

    date_str = dt.strftime("%Y-%m-%d")
    if total_seconds < 0:
        return date_str
    if total_seconds < 60:
        rel = "just now"
    elif total_seconds < 3600:
        mins = total_seconds // 60
        rel = f"{mins}m ago" if mins > 1 else "1m ago"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        rel = f"{hours}h ago" if hours > 1 else "1h ago"
    elif total_seconds < 86400 * 30:
        days = total_seconds // 86400
        rel = f"{days}d ago" if days > 1 else "1d ago"
    elif total_seconds < 86400 * 365:
        months = total_seconds // (86400 * 30)
        rel = f"{months}mo ago" if months > 1 else "1mo ago"
    else:
        years = total_seconds // (86400 * 365)
        rel = f"{years}y ago" if years > 1 else "1y ago"

    if short:
        return rel
    return f"{rel} ({date_str})"


def truncate(value: object, width: int) -> str:
    """Fit a value in an fzf column without letting it push other columns away."""
    text = str(value or "")
    return text if len(text) <= width else text[:width - 1] + "…"


def format_row(skill: dict) -> str:
    """Render the visible, fixed-width part of a live fzf result."""
    name = truncate(skill.get("name"), NAME_WIDTH)
    source = truncate(skill.get("source") or skill.get("id"), SOURCE_WIDTH)
    metric = format_metric(skill.get("installs"), skill.get("stars"))
    updated = truncate(format_relative_time(skill.get("updated_at"), short=True) or "-", UPDATED_WIDTH)
    remote = truncate(skill.get("remote") or "remote", REMOTE_WIDTH)
    colors = row_colors()
    reset = colors["reset"]
    dim = colors.get("dim", "\x1b[2m")
    upd_color = colors.get("updated", dim)
    return (
        f"{colors['name']}{name:<{NAME_WIDTH}}{reset} "
        f"{colors['description']}{metric:<{METRIC_WIDTH}}{reset} "
        f"{colors['location']}{source:<{SOURCE_WIDTH}}{reset} "
        f"{upd_color}{updated:<{UPDATED_WIDTH}}{reset} "
        f"{dim}{remote:<{REMOTE_WIDTH}}{reset}"
    )


def choose(query: str = "", owner: Optional[str] = None, remotes: Optional[list[str]] = None) -> list[dict]:
    if shutil.which("fzf") and sys.stdin.isatty():
        return choose_with_fzf(query, owner, remotes=remotes)
    if not query:
        try:
            query = Prompt.ask("Search skills", console=console).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return []
    results = search(query, owner, remotes=remotes)
    if not results:
        print_warn("No skills found.")
        return []
    table = Table(title=f"Remote results for '{query}'", header_style="header")
    table.add_column("#", justify="right")
    table.add_column("Skill", style="bold")
    table.add_column("Popularity", justify="right")
    table.add_column("Source")
    table.add_column("Updated", justify="right")
    table.add_column("Remote")
    for index, skill in enumerate(results, 1):
        pop = format_metric(skill.get("installs"), skill.get("stars"))
        upd = format_relative_time(skill.get("updated_at"), short=True) or "-"
        table.add_row(
            str(index),
            str(skill.get("name", "")),
            pop,
            str(skill.get("source") or skill.get("id", "")),
            upd,
            str(skill.get("remote", "")),
        )
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


def cached_search(query: str, cache_dir: Path, owner: Optional[str] = None, remotes: Optional[list[str]] = None) -> list[dict]:
    """search(), remembering each query for the lifetime of one picker session."""
    remotes_str = ",".join(sorted(remotes)) if remotes else ""
    key = hashlib.sha256(f"{query}\0{owner or ''}\0{remotes_str}".encode()).hexdigest()
    path = cache_dir / f"query-{key}.json"
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    skills = search(query, owner, remotes=remotes)
    if skills:
        try:
            path.write_text(json.dumps(skills), encoding="utf-8")
        except OSError:
            pass
    return skills


def write_rows(query: str, cache_dir: Path, owner: Optional[str] = None, remotes: Optional[list[str]] = None) -> list[str]:
    """Write fzf candidates and their preview inputs for one query across remotes."""
    if len(query.strip()) < MIN_QUERY_LENGTH:
        return []
    rows = []
    for skill in cached_search(query.strip(), cache_dir, owner, remotes=remotes):
        key = f"{skill.get('source') or skill.get('id', '')}:{skill.get('name', '')}:{skill.get('remote', '')}"
        path = cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
        path.write_text(json.dumps(skill), encoding="utf-8")
        rows.append(f"{format_row(skill)}\t{path}")
    return rows


def choose_with_fzf(query: str, owner: Optional[str] = None, remotes: Optional[list[str]] = None) -> list[dict]:
    """Browse live remote search results with details loaded for the highlighted row."""
    with tempfile.TemporaryDirectory(prefix="skctl-search-") as temporary:
        root = Path(temporary)
        preview = preview_command()
        rows = f"{shell_quote(sys.executable)} -m skill_ctl.catalog --rows --cache-dir {shell_quote(str(root))} --query {{q}}"
        if owner:
            rows += f" --owner {shell_quote(owner)}"
        if remotes:
            for r in remotes:
                rows += f" --remote {shell_quote(r)}"

        debounced_rows = f"{rows} --debounce {SEARCH_DEBOUNCE}"
        header = (
            f"{'Skill':<{NAME_WIDTH}} {'Popularity':<{METRIC_WIDTH}} {'Source':<{SOURCE_WIDTH}} {'Updated':<{UPDATED_WIDTH}} Remote\n"
            "Tab · ctrl-a all · ctrl-d none · Alt-A repo · Alt-C query"
        )
        result = subprocess.run(
            [
                "fzf", "--multi", "--ansi", "--phony", "--disabled", "--query", query,
                "--delimiter", "\t", "--with-nth", "1",
                "--header", header,
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
    width_var = "%FZF_PREVIEW_COLUMNS%" if os.name == "nt" else '"$FZF_PREVIEW_COLUMNS"'
    return (
        f"{command} | {renderer} --color=always --paging=never --style=plain "
        f"--language=md --theme={theme} --squeeze-blank --wrap=character "
        f"--terminal-width={width_var}"
    )


def github_source(source: str) -> Optional[str]:
    return parse_github_repo(source)


def _fetch_raw_github_file(
    repo: str, path: str, branch: str = "main", timeout: float = 3.5
) -> Optional[str]:
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    headers = {"User-Agent": "skill-ctl"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _fetch_candidates_parallel(
    repo: str, candidates: tuple[str, ...]
) -> tuple[Optional[str], Optional[str]]:
    """Fetch candidate SKILL.md paths in parallel across main, master, and HEAD branches."""
    branches = ("main", "master", "HEAD")
    results: dict[int, tuple[str, str]] = {}
    done_ranks: set[int] = set()

    with ThreadPoolExecutor(max_workers=min(len(candidates) * len(branches), 8)) as pool:
        futures = {}
        for rank, path in enumerate(candidates):
            for branch in branches:
                fut = pool.submit(_fetch_raw_github_file, repo, path, branch, 3.0)
                futures[fut] = (rank, path)

        for fut in as_completed(futures):
            rank, path = futures[fut]
            try:
                content = fut.result()
            except Exception:
                content = None

            if content is not None and rank not in results:
                results[rank] = (path, content)
                done_ranks.add(rank)

            rank_futures = [f for f, (r, _) in futures.items() if r == rank]
            if all(f.done() for f in rank_futures):
                done_ranks.add(rank)

            if results:
                best_rank = min(results.keys())
                if all(r in done_ranks for r in range(best_rank)):
                    for f in futures:
                        if not f.done():
                            f.cancel()
                    return results[best_rank]

    if results:
        best_rank = min(results.keys())
        return results[best_rank]
    return None, None


def frontmatter(source: str, skill_name: str) -> dict:
    repo = github_source(source)
    if not repo:
        return {}

    candidates = (f"{skill_name}/SKILL.md", f"skills/{skill_name}/SKILL.md", "SKILL.md")
    path, text = _fetch_candidates_parallel(repo, candidates)

    if text is None:
        try:
            tree = get_json(f"{GITHUB_API}/repos/{repo}/git/trees/HEAD?recursive=1").get("tree", [])
            blob_candidates = [
                entry["path"]
                for entry in tree
                if entry.get("type") == "blob"
                and (entry.get("path") == "SKILL.md" or entry.get("path", "").endswith("/SKILL.md"))
            ]
            exact = [
                p
                for p in blob_candidates
                if p != "SKILL.md" and p.rsplit("/", 2)[-2].lower() == skill_name.lower()
            ]
            if exact:
                path = exact[0]
            elif blob_candidates and (len(blob_candidates) == 1 or "SKILL.md" in blob_candidates):
                path = "SKILL.md" if "SKILL.md" in blob_candidates else blob_candidates[0]
            else:
                return {}
            text = _fetch_raw_github_file(repo, path, branch="HEAD", timeout=6.0)
            if not text:
                return {}
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
    """Collect the information worth reading before install."""
    name = str(skill.get("name", ""))
    source = str(skill.get("source") or skill.get("id", ""))
    repo = parse_github_repo(source) or github_source(source)
    remote = skill.get("remote", "skills.sh")

    def fetch_repo_info() -> tuple[Optional[int], Optional[str]]:
        cached_stars = skill.get("stars")
        cached_updated = skill.get("updated_at")
        if cached_stars is not None and cached_updated is not None:
            return cached_stars, cached_updated
        if not repo:
            return cached_stars, cached_updated
        try:
            repo_data = get_json(f"{GITHUB_API}/repos/{repo}")
            stars = cached_stars if cached_stars is not None else repo_data.get("stargazers_count")
            updated = cached_updated or repo_data.get("pushed_at") or repo_data.get("updated_at")
            return stars, updated
        except (OSError, URLError, ValueError):
            return cached_stars, cached_updated

    def fetch_meta() -> dict:
        meta = frontmatter(source, name)
        if not meta.get("description") and skill.get("description"):
            meta["description"] = skill.get("description")
        return meta

    with ThreadPoolExecutor(max_workers=2) as executor:
        repo_fut = executor.submit(fetch_repo_info)
        meta_fut = executor.submit(fetch_meta)
        stars, updated_at = repo_fut.result()
        metadata = meta_fut.result()

    if not updated_at and metadata:
        updated_at = metadata.get("updated") or metadata.get("updated_at") or metadata.get("date")

    return {
        "name": name,
        "source": source,
        "remote": remote,
        "installs": format_number(skill.get("installs")) if skill.get("installs") is not None else None,
        "stars": stars,
        "updated_at": updated_at,
        "updated": format_relative_time(updated_at),
        "metadata": metadata,
        "url": skill.get("url") or (f"{SKILLS_URL}/{skill.get('id', '')}" if "skills.sh" in remote else ""),
    }


def detail_lines(data: dict) -> list[str]:
    metadata = data.get("metadata", {})
    lines = [
        data.get("name", ""),
        "",
        f"Remote: {data.get('remote', 'skills.sh')}",
        f"Source: {data.get('source', '')}",
    ]
    if data.get("installs") is not None and data.get("installs") != "-":
        lines.append(f"Installs: {data.get('installs')}")
    if data.get("stars") is not None:
        lines.append(f"GitHub stars: {data['stars']:,}")
    if data.get("updated"):
        lines.append(f"Updated: {data['updated']}")
    desc = data.get("description") or metadata.get("description")
    if desc:
        lines.extend(("", "Description:", " ".join(str(desc).split())))
    if metadata.get("path"):
        lines.append(f"SKILL.md: {metadata['path']}")
    if data.get("url"):
        lines.extend(("", f"Details: {data.get('url')}"))
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
    """Show what was picked, then ask once whether to install it."""
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
            remote = str(skill.get("remote", ""))
            pop = format_metric(skill.get("installs"), skill.get("stars"))
            console.print(f"  [header]{name}[/header] [dim]({source} · {pop} · {remote})[/dim]")
        prompt = f"\nInstall {len(skills)} skills?"
    try:
        return Confirm.ask(prompt, default=True, console=console)
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) == 3 and sys.argv[1] == "--preview-file":
        print("\n".join(detail_lines(read_preview(Path(sys.argv[2])))))
    elif len(sys.argv) == 3 and sys.argv[1] == "--preview-line":
        fields = sys.argv[2].split("\t")
        if len(fields) < 2:
            print(f"Type at least {MIN_QUERY_LENGTH} characters to search remote catalogs.")
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
        parser.add_argument("--remote", action="append", dest="remotes")
        parser.add_argument("--debounce", type=float, default=0.0)
        args = parser.parse_args()
        if args.debounce > 0:
            import time

            time.sleep(args.debounce)
        print("\n".join(write_rows(args.query, Path(args.cache_dir), args.owner, remotes=args.remotes)))
    else:
        raise SystemExit("catalog preview is only available through `skctl search`")
