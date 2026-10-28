"""Backing ~/.skill-ctl up to a GitHub repository (backup init/push/pull/status)."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Optional

import typer

from skill_ctl.config import ensure_config, load_config
from skill_ctl.constants import BASE_DIR
from skill_ctl.linker import links_into_preset, prune_empty_dirs, prune_lockfile
from skill_ctl.ui import console, print_error, print_header, print_success, print_warn

BRANCH = "main"
DEFAULT_REPO_NAME = "skill-ctl-presets"

DEFAULT_GLOBAL_REL_DIRS = (
    ".agents/skills",
    ".claude/skills",
    ".cursor/skills",
    ".codex/skills",
    ".hermes/skills",
    ".gemini/skills",
    ".codeium/windsurf/skills",
    ".windsurf/skills",
)

GITIGNORE = """# Ignore all root-level entries by default so cache, machine config, and future files stay local
/*

# Whitelist only presets, global skills, and gitignore
!/.gitignore
!/presets/
!/global/

# Ignore noise within presets and global
node_modules/
__pycache__/
*.pyc
.DS_Store
"""


def _links_into_presets(path: Path, presets_dir: Path) -> bool:
    if not path.is_symlink() or not presets_dir.is_dir():
        return False
    if links_into_preset(path, presets_dir):
        return True
    return any(
        preset.is_dir() and links_into_preset(path, preset) for preset in presets_dir.iterdir()
    )


def _global_rel_dirs(config: Optional[dict] = None) -> list[str]:
    """Relative home paths for configured global skill directories (preserving symlinked dirs)."""
    cfg = config or load_config()
    home = Path.home()
    rel_dirs: list[str] = []
    configured = cfg.get("global_skill_dirs", [])
    if isinstance(configured, list):
        for value in configured:
            if not isinstance(value, str) or not value.strip():
                continue
            expanded = Path(value).expanduser()
            try:
                rel = expanded.relative_to(home).as_posix()
            except ValueError:
                continue
            if rel and rel not in rel_dirs:
                rel_dirs.append(rel)
    return rel_dirs or list(DEFAULT_GLOBAL_REL_DIRS)


def collect_local_global_skills_by_dir(
    config: Optional[dict] = None,
) -> dict[str, dict[str, Path]]:
    """Return standalone global skills grouped by relative agent dir (e.g. '.cursor/skills')."""
    presets_dir = BASE_DIR / "presets"
    home = Path.home()
    by_dir: dict[str, dict[str, Path]] = {}

    for rel_dir in _global_rel_dirs(config):
        directory = home / rel_dir
        if not directory.is_dir():
            continue
        skills: dict[str, Path] = {}
        try:
            with os.scandir(directory) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    if entry.name.startswith("."):
                        continue
                    entry_path = Path(entry.path)
                    if _links_into_presets(entry_path, presets_dir):
                        continue
                    if not entry_path.is_dir():
                        continue
                    if (entry_path / "SKILL.md").is_file():
                        skills[entry.name] = entry_path.resolve()
        except OSError:
            pass
        if skills:
            by_dir[rel_dir] = skills
    return by_dir


def collect_local_global_skills(config: Optional[dict] = None) -> dict[str, Path]:
    """Return deduplicated standalone global skills keyed by skill name."""
    skills: dict[str, Path] = {}
    for dir_skills in collect_local_global_skills_by_dir(config).values():
        for name, path in dir_skills.items():
            skills.setdefault(name, path)
    return skills


def backed_up_global_skills_by_dir() -> dict[str, dict[str, Path]]:
    """Return global skills currently staged in ~/.skill-ctl/global grouped by relative agent dir."""
    global_root = BASE_DIR / "global"
    if not global_root.is_dir():
        return {}
    by_dir: dict[str, dict[str, Path]] = {}
    for skill_md in sorted(global_root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        if skill_dir.name.startswith(".") or skill_dir.parent == global_root:
            continue
        rel_dir = skill_dir.parent.relative_to(global_root).as_posix()
        by_dir.setdefault(rel_dir, {})[skill_dir.name] = skill_dir
    return by_dir


def backed_up_global_skills() -> dict[str, Path]:
    """Return deduplicated global skills staged in ~/.skill-ctl/global."""
    skills: dict[str, Path] = {}
    for dir_skills in backed_up_global_skills_by_dir().values():
        for name, path in dir_skills.items():
            skills.setdefault(name, path)
    return skills


def backed_up_global_entries() -> set[tuple[str, str]]:
    """Return (rel_dir, skill_name) pairs currently staged in ~/.skill-ctl/global."""
    return {
        (rel_dir, name)
        for rel_dir, skills in backed_up_global_skills_by_dir().items()
        for name in skills
    }


def _dir_digest(root: Path) -> str:
    """Fingerprint the files copied into a global skill backup."""
    ignored = {".git", "node_modules", "__pycache__", ".DS_Store"}
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in ignored or part.endswith(".pyc") for part in rel.parts):
            continue
        if path.is_file():
            try:
                content_digest = hashlib.sha256(path.read_bytes()).digest()
            except OSError:
                continue
            digest.update(rel.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(content_digest)
    return digest.hexdigest()


def _dirs_differ(left: Path, right: Path) -> bool:
    """Compare two skill directories by relative file paths and contents."""
    return _dir_digest(left) != _dir_digest(right)


def _global_sync_state() -> dict[str, dict[str, str]]:
    """The last installed global snapshot on this device; never backed up."""
    try:
        data = json.loads((BASE_DIR / "global-sync.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        directory: {
            name: value
            for name, value in skills.items()
            if isinstance(value, str)
            and name
            and not name.startswith(".")
            and "/" not in name
            and "\\" not in name
        }
        for directory, skills in data.items()
        if isinstance(skills, dict)
    }


def global_sync_entries() -> set[tuple[str, str]]:
    return {
        (directory, name) for directory, skills in _global_sync_state().items() for name in skills
    }


def _remember_global_skills(config: Optional[dict] = None) -> None:
    state = _global_sync_state()
    local_by_dir = collect_local_global_skills_by_dir(config)
    for directory in _global_rel_dirs(config):
        state[directory] = {
            name: _dir_digest(path) for name, path in local_by_dir.get(directory, {}).items()
        }
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    path = BASE_DIR / "global-sync.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _global_skill_changes(
    config: Optional[dict] = None, sync: bool = False
) -> list[tuple[str, str, str, Optional[Path]]]:
    local_by_dir = collect_local_global_skills_by_dir(config)
    staged_by_dir = backed_up_global_skills_by_dir()
    baseline = _global_sync_state() if sync else {}
    changes = []
    for directory in sorted(_global_rel_dirs(config)):
        local = local_by_dir.get(directory, {})
        staged = staged_by_dir.get(directory, {})
        previous = baseline.get(directory, {})
        for name in sorted(set(local) | set(staged) | set(previous)):
            local_digest = _dir_digest(local[name]) if name in local else None
            staged_digest = _dir_digest(staged[name]) if name in staged else None
            if sync:
                previous_digest = previous.get(name)
                if local_digest == previous_digest:
                    continue
                if staged_digest not in (previous_digest, local_digest):
                    changes.append(("C", directory, name, local.get(name)))
                    continue
            if local_digest != staged_digest:
                action = "D" if name not in local else ("A" if name not in staged else "M")
                changes.append((action, directory, name, local.get(name)))
    return changes


def uncommitted_global_changes(config: Optional[dict] = None, sync: bool = False) -> list[str]:
    """Compare local global skills with ~/.skill-ctl/global without modifying disk."""
    return [
        f"{action}\tglobal/{directory}/{name}"
        for action, directory, name, _ in _global_skill_changes(config, sync)
    ]


def stage_global_skills(
    config: Optional[dict] = None, sync: bool = False
) -> tuple[list[str], list[str]]:
    """Mirror standalone global skills across all agent directories into ~/.skill-ctl/global.

    Only folders listed in this device's global_skill_dirs are touched, so a device
    with a narrower config never deletes another device's backed-up folders.
    Sync compares local changes with this device's last installed snapshot.
    """
    changes = _global_skill_changes(config, sync)
    conflicts = [
        f"global/{directory}/{name}" for action, directory, name, _ in changes if action == "C"
    ]
    if conflicts:
        print_error("Global skills changed both locally and in the backup; sync stopped.")
        for path in conflicts:
            console.print(f"  {path}", markup=False)
        print_warn(
            f"Reconcile the copies in your global skill folders and {BASE_DIR / 'global'}, then retry."
        )
        raise SystemExit(1)
    global_root = BASE_DIR / "global"
    removed_names: set[str] = set()
    ignore_patterns = shutil.ignore_patterns(
        ".git", "node_modules", "__pycache__", ".DS_Store", "*.pyc"
    )
    all_local_names = {
        name for skills in collect_local_global_skills_by_dir(config).values() for name in skills
    }
    for action, directory, name, src_path in changes:
        dst_path = global_root / directory / name
        if dst_path.is_symlink():
            dst_path.unlink()
        elif dst_path.exists():
            shutil.rmtree(dst_path)
        if action == "D":
            removed_names.add(name)
            prune_empty_dirs(dst_path.parent, global_root)
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_path, dst_path, ignore=ignore_patterns)

    # Sync global .skill-lock.json entries for the backed-up global skills
    home_lock = Path.home() / ".agents" / ".skill-lock.json"
    staged_lock = global_root / ".agents" / ".skill-lock.json"
    merged_entries: dict = {}
    changed_names = {name for action, _, name, _ in changes if action in ("A", "M")}
    for lock_path in (staged_lock, home_lock):
        if lock_path.is_file():
            try:
                raw = json.loads(lock_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("skills"), dict):
                    entries = raw["skills"]
                    if sync and lock_path == home_lock:
                        entries = {
                            name: value
                            for name, value in entries.items()
                            if name in changed_names or name not in merged_entries
                        }
                    merged_entries.update(entries)
            except (OSError, ValueError):
                pass
    still_staged = {name for skills in backed_up_global_skills_by_dir().values() for name in skills}
    kept_entries = {k: v for k, v in merged_entries.items() if k in still_staged}
    if kept_entries:
        staged_lock.parent.mkdir(parents=True, exist_ok=True)
        staged_lock.write_text(
            json.dumps({"version": 1, "skills": kept_entries}, indent=2) + "\n",
            encoding="utf-8",
        )
    elif staged_lock.exists():
        staged_lock.unlink()
        prune_empty_dirs(staged_lock.parent, global_root)

    if not still_staged and global_root.exists():
        shutil.rmtree(global_root, ignore_errors=True)

    if not sync:
        _remember_global_skills(config)
    return sorted(all_local_names), sorted(removed_names - all_local_names)


def restore_global_skills(
    removed_names: Optional[set[str]] = None,
    force: bool = False,
    config: Optional[dict] = None,
    removed_entries: Optional[set[tuple[str, str]]] = None,
) -> int:
    """Restore global skills from ~/.skill-ctl/global into global agent directories."""
    presets_dir = BASE_DIR / "presets"
    home = Path.home()
    configured = set(_global_rel_dirs(config))
    staged_by_dir = {
        rel_dir: skills
        for rel_dir, skills in backed_up_global_skills_by_dir().items()
        if rel_dir in configured
    }

    # Handle explicit per-directory or skill-name removals
    entries_to_remove: set[tuple[str, str]] = {
        entry for entry in (removed_entries or ()) if entry[0] in configured
    }
    if removed_names:
        for rel_dir in _global_rel_dirs(config):
            for name in removed_names:
                entries_to_remove.add((rel_dir, name))

    if entries_to_remove:
        agents_root_resolved = (home / ".agents" / "skills").resolve()
        for rel_dir, name in sorted(entries_to_remove):
            gdir = home / rel_dir
            if not gdir.is_dir():
                continue
            # If gdir (e.g. ~/.claude/skills) is a directory symlink to ~/.agents/skills,
            # do not delete the target skill unless it was also removed from .agents/skills.
            if (
                rel_dir != ".agents/skills"
                and gdir.resolve() == agents_root_resolved
                and (".agents/skills", name) not in entries_to_remove
                and name in staged_by_dir.get(".agents/skills", {})
            ):
                continue
            candidate = gdir / name
            if not (candidate.is_symlink() or candidate.exists()):
                continue
            if _links_into_presets(candidate, presets_dir) and not force:
                continue
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
            else:
                shutil.rmtree(candidate)
        still_present = {name for skills in staged_by_dir.values() for name in skills}
        completely_removed = {name for _, name in entries_to_remove} - still_present
        if completely_removed:
            prune_lockfile(home, completely_removed)

    restored_names: set[str] = set()
    restored_dirs: list[str] = []

    # 1. Restore ~/.agents/skills first so other agent dirs can link to it when identical
    agents_skills = staged_by_dir.get(".agents/skills", {})
    agents_target_dir = home / ".agents" / "skills"
    if agents_skills:
        restored_dirs.append("~/.agents/skills")
        for name, src_path in sorted(agents_skills.items()):
            dst = agents_target_dir / name
            if dst.is_symlink():
                if _links_into_presets(dst, presets_dir) and not force:
                    print_warn(
                        f"Kept existing preset link ~/.agents/skills/{name} (use --force to replace)"
                    )
                    continue
                dst.unlink()
            elif dst.exists():
                if not _dirs_differ(src_path, dst):
                    restored_names.add(name)
                    continue
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_path, dst)
            restored_names.add(name)

    # 2. Restore all other agent directories (.claude/skills, .cursor/skills, .hermes/skills, etc.)
    agents_resolved = agents_target_dir.resolve() if agents_target_dir.exists() else None
    for rel_dir, skills in sorted(staged_by_dir.items()):
        if rel_dir == ".agents/skills" or not skills:
            continue
        target_dir = home / rel_dir
        restored_dirs.append(f"~/{rel_dir}")
        # If the agent dir itself is a symlink to ~/.agents/skills, only copy skills not in .agents/skills
        if agents_resolved and target_dir.exists() and target_dir.resolve() == agents_resolved:
            for name, src_path in sorted(skills.items()):
                if name in agents_skills:
                    restored_names.add(name)
                    continue
                dst = target_dir / name
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(src_path, dst)
                restored_names.add(name)
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        for name, src_path in sorted(skills.items()):
            dst = target_dir / name
            if dst.is_symlink():
                if _links_into_presets(dst, presets_dir) and not force:
                    print_warn(
                        f"Kept existing preset link ~/{rel_dir}/{name} (use --force to replace)"
                    )
                    continue
                dst.unlink()
            elif dst.exists():
                shutil.rmtree(dst)

            canonical = agents_target_dir / name
            linked = False
            if canonical.is_dir() and not _dirs_differ(src_path, canonical):
                rel_target = os.path.relpath(canonical, target_dir)
                try:
                    dst.symlink_to(rel_target, target_is_directory=True)
                    linked = True
                except OSError:
                    linked = False
            if not linked:
                shutil.copytree(src_path, dst)
            restored_names.add(name)

    staged_lock = BASE_DIR / "global" / ".agents" / ".skill-lock.json"
    if staged_lock.is_file():
        try:
            staged_data = json.loads(staged_lock.read_text(encoding="utf-8"))
            staged_skills = staged_data.get("skills", {}) if isinstance(staged_data, dict) else {}
            if isinstance(staged_skills, dict) and staged_skills:
                home_lock = home / ".agents" / ".skill-lock.json"
                home_data: dict = {"version": 1, "skills": {}}
                if home_lock.is_file():
                    try:
                        loaded = json.loads(home_lock.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict) and isinstance(loaded.get("skills"), dict):
                            home_data = loaded
                    except (OSError, ValueError):
                        pass
                home_data["skills"].update(staged_skills)
                home_lock.parent.mkdir(parents=True, exist_ok=True)
                home_lock.write_text(json.dumps(home_data, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError):
            pass

    _remember_global_skills(config)
    if restored_names:
        dirs_label = ", ".join(restored_dirs) if restored_dirs else "~/.agents/skills"
        print_success(
            f"Restored {len(restored_names)} global skill{'s' if len(restored_names) != 1 else ''} into {dirs_label}"
        )
    return len(restored_names)


def git(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    """Run git inside ~/.skill-ctl."""
    if shutil.which("git") is None:
        print_error("'git' not found on PATH. Install git and retry.")
        sys.exit(1)
    return subprocess.run(
        ["git", "-C", str(BASE_DIR)] + args,
        capture_output=capture,
        text=True,
        # Git's raw UTF-8 paths must not use Windows' locale-dependent codec.
        encoding="utf-8",
        errors="replace",
    )


def has_repo() -> bool:
    return (BASE_DIR / ".git").is_dir()


@contextmanager
def backup_lock():
    """Serialize backup and sync operations, including background pushes."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    with (BASE_DIR / ".backup.lock").open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt

            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)

            def acquire():
                return msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                return msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                return fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                return fcntl.flock(lock, fcntl.LOCK_UN)

        try:
            acquire()
        except OSError:
            print_error("Another backup or sync is running. Try again when it finishes.")
            raise SystemExit(1)
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock.seek(0)
            release()


def sanitize_url(url: Optional[str]) -> str:
    """Mask embedded credentials in a git URL to prevent logging secrets."""
    if not url:
        return ""
    return re.sub(r"://([^@:]+)(:[^@]*)?@", "://***@", url)


def remote_url() -> Optional[str]:
    if not has_repo():
        return None
    result = git(["remote", "get-url", "origin"], capture=True)
    return result.stdout.strip() if result.returncode == 0 else None


def repo_url(repo: str) -> str:
    """Accept a full URL, 'owner/name', or a bare name (which needs gh to create)."""
    if not repo or repo.startswith("-"):
        print_error(f"Invalid repository '{repo}': cannot start with '-'.")
        sys.exit(1)
    if "://" in repo or repo.startswith("git@"):
        return repo
    if repo.count("/") == 1:
        return f"https://github.com/{repo}.git"
    print_error(f"Cannot tell who owns '{repo}'. Pass owner/name or a full URL.")
    sys.exit(1)


def ensure_local_repo() -> None:
    """Make ~/.skill-ctl a git repo, without touching any remote."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    ensure_config()
    if not has_repo():
        if git(["init", "-b", BRANCH]).returncode != 0:
            sys.exit(1)
        print_success(f"Initialized git repository at {BASE_DIR}")
    gitignore = BASE_DIR / ".gitignore"
    if (
        not gitignore.exists()
        or "!/presets/" not in gitignore.read_text(encoding="utf-8")
        or "!/global/" not in gitignore.read_text(encoding="utf-8")
    ):
        gitignore.write_text(GITIGNORE, encoding="utf-8")


def keep_empty_presets() -> None:
    """Give every empty preset a .gitkeep, since git stores no empty directories.

    A preset you made but have not filled yet would otherwise vanish on restore.
    """
    presets_dir = BASE_DIR / "presets"
    if not presets_dir.is_dir():
        return
    for preset in presets_dir.iterdir():
        if not preset.is_dir():
            continue
        if any(p.is_file() for p in preset.rglob("*")):
            continue
        try:
            (preset / ".gitkeep").touch()
        except OSError as e:
            print_warn(f"Could not mark empty preset '{preset.name}': {e}")


def set_remote(url: str) -> None:
    verb = "set-url" if remote_url() else "add"
    if git(["remote", verb, "origin", url]).returncode != 0:
        sys.exit(1)


def create_github_repo(name: str, private: bool) -> Optional[str]:
    """Create the backup repo with the gh CLI and wire it up as origin."""
    if shutil.which("gh") is None:
        return None
    print_header(f"Creating {'private' if private else 'public'} GitHub repo '{name}' via gh")
    result = subprocess.run(
        ["gh", "repo", "create", name, "--private" if private else "--public"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print((result.stderr or result.stdout).rstrip())
        return None

    # gh prints the new repo's URL; fall back to composing it from the account.
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("http")]
    url = lines[-1].strip() if lines else None
    if not url:
        owner = github_login()
        if not owner:
            return None
        url = f"https://github.com/{owner}/{name}"
    if not url.endswith(".git"):
        url += ".git"
    set_remote(url)
    return url


def backup(
    action: Annotated[Literal["push", "pull", "init", "status"], typer.Argument()] = "push",
    repo: Annotated[
        Optional[str],
        typer.Option(
            "--repo",
            "-r",
            help="Backup repository: owner/name, a full git URL, or a bare name to create with gh.",
        ),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(
            "--message", "-m", help="Commit message for 'push' (default: 'backup presets')."
        ),
    ] = None,
    public: Annotated[
        bool, typer.Option("--public", help="Create the gh repo public instead of private.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the confirmation when a pull or --force push overwrites presets.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show incoming backup changes without restoring them.")
    ] = False,
    include_config: Annotated[
        bool,
        typer.Option(
            "--config",
            help="Also back up config.yaml (excluded by default; it's per-machine preference).",
        ),
    ] = False,
    include_global: Annotated[
        bool,
        typer.Option(
            "--global",
            "-g",
            help="Also back up or restore global skills (~/.agents/skills, ~/.claude/skills, ...).",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="On 'push', overwrite the remote with what's here. On 'pull', discard local commits and take the "
            "remote's instead. Either way, whichever side loses commits loses them for good; confirms first.",
        ),
    ] = False,
) -> None:
    """Back up ~/.skill-ctl (presets, not config.yaml) to a GitHub repository.

    `init` points it at a repo, `push` commits and uploads, `pull` restores on
    another machine, `status` shows what is not backed up yet.

    Pass --global / -g to also snapshot and restore global skills
    (~/.agents/skills, ~/.claude/skills, etc.).

    config.yaml and applied.json stay local: config.yaml holds per-machine
    preferences (theme, apply targets, default preset) and applied.json holds
    this machine's absolute project paths, so restoring them on another
    machine would fight its own setup rather than help it. Pass --config to
    a push to include config.yaml anyway; applied.json never syncs.

    A push rejected because the remote has commits you don't (e.g. pushed
    from another machine) normally means `skctl backup pull` first. --force
    skips that and overwrites the remote with what's here instead - use it
    when this machine's presets are the ones you want to keep.
    """
    if action == "status":
        backup_status(include_global=include_global)
        return
    if action == "pull" and dry_run:
        backup_pull(repo, yes, dry_run, force, include_global=include_global)
        return
    with backup_lock():
        if action == "init":
            backup_init(repo, public, yes)
        elif action == "push":
            backup_push(
                message,
                repo,
                public,
                include_config,
                force,
                yes,
                include_global=include_global,
            )
        else:
            backup_pull(repo, yes, dry_run, force, include_global=include_global)


def backup_init(repo: Optional[str], public: bool, yes: bool = False) -> None:
    ensure_local_repo()
    current = remote_url()

    if repo and "/" not in repo and "://" not in repo:
        # A bare name is a repo to create, not one to point at.
        if current:
            print_warn(f"Repointing the backup away from {current}.")
        url = create_github_repo(repo, not public)
        if not url:
            print_error("Could not create the repo (is the gh CLI installed and logged in?).")
            print_warn("Create one yourself, then: skctl backup init --repo owner/name")
            sys.exit(1)
    elif repo:
        url = repo_url(repo)
        set_remote(url)
    elif current:
        url = current
    else:
        url = create_github_repo(DEFAULT_REPO_NAME, not public)
        if not url:
            print_error("No backup repository configured.")
            print_warn(
                "Run: skctl backup init --repo owner/name   (or install the gh CLI to create one)"
            )
            sys.exit(1)

    maybe_reset_local_history(current, url, yes)

    print_success(f"Backing up {BASE_DIR} to {sanitize_url(url)}")
    print_warn("Presets are uploaded as-is; use a private repo if any skill is not public.")
    step, note = suggest_next_step(url)
    console.print(f"Next: [header]{step}[/header]  [dim]({note})[/dim]")


def maybe_reset_local_history(old_url: Optional[str], new_url: str, yes: bool) -> None:
    """Offer to drop this machine's git history when `init` repoints it at a
    different repo than before.

    Carrying the old repo's commits over is what used to send `push`/`pull`
    down the wrong path: the new remote has unrelated history, so a plain
    push gets rejected ("fetch first") and `suggest_next_step` reasons about
    the wrong repo's state. A repo `init` has never pointed at before has
    nothing to reset for.
    """
    if not old_url or old_url == new_url:
        return
    if git(["rev-parse", "--verify", "HEAD"], capture=True).returncode != 0:
        return  # nothing committed yet, nothing to lose
    print_warn(
        f"{BASE_DIR} still has git history from the previous backup repo ({sanitize_url(old_url)})."
    )
    if not yes:
        if not sys.stdin.isatty():
            print_warn(
                "Keeping that history (not a terminal to confirm dropping it); pass --yes to drop it automatically."
            )
            return
        try:
            from rich.prompt import Confirm

            if not Confirm.ask(
                "Start fresh for the new repo (drop that local history)?",
                default=True,
                console=console,
            ):
                return
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
    shutil.rmtree(BASE_DIR / ".git")
    if git(["init", "-b", BRANCH]).returncode != 0:
        sys.exit(1)
    set_remote(new_url)
    print_success("Started a fresh local history for the new backup repo.")


def suggest_next_step(url: str) -> tuple[str, str]:
    """The command that matches this repo's state, plus the other one as a
    short alternative: guessing wrong here is exactly what used to cause the
    'fetch first' / 'Access is denied' tangle, so both stay visible."""
    remote_has_commits = (
        git(["ls-remote", "--exit-code", "--heads", url, BRANCH], capture=True).returncode == 0
    )
    if remote_has_commits:
        return "skctl backup pull", "or push --force"
    return "skctl backup push", "or pull"


def untrack_if_tracked(rel_path: str) -> None:
    """Drop a file or directory from the next commit without deleting it from disk.

    Handles backups made before config.yaml/applied.json/cache were gitignored:
    once tracked, `git add -A` keeps re-adding a file regardless of
    .gitignore, so it must be explicitly untracked once.
    """
    res = git(["ls-files", rel_path], capture=True)
    if res.returncode == 0 and res.stdout.strip():
        git(["rm", "-r", "--cached", "-q", rel_path])


def backup_push(
    message: Optional[str],
    repo: Optional[str],
    public: bool,
    include_config: bool = False,
    force: bool = False,
    yes: bool = False,
    include_global: bool = False,
) -> None:
    # backup_init calls ensure_local_repo itself, so only the already-set-up
    # path needs it here.
    if not has_repo() or not remote_url():
        backup_init(repo, public, yes)
    else:
        ensure_local_repo()

    if include_global:
        stage_global_skills()

    default_msg = "backup presets and global skills" if include_global else "backup presets"
    commit_presets(message or default_msg, include_config)

    url = remote_url()
    push_args = ["push", "-u", "origin", BRANCH]
    if force:
        # Fetch first so --force-with-lease has a remote-tracking ref to
        # compare against: it then refuses if the remote has moved again
        # since, instead of blindly clobbering whatever is there right now.
        git(["fetch", "origin", BRANCH], capture=True)
        if not confirm_force_push(url, yes):
            print_warn("Force-push cancelled.")
            return
        push_args.insert(1, "--force-with-lease")

    print_header(f"Pushing {BASE_DIR} to {sanitize_url(url)}")
    result = git(push_args, capture=True)
    if result.returncode != 0:
        console.print((result.stderr or result.stdout).rstrip())
        explain_push_failure(result.stderr or "", url or "")
        sys.exit(result.returncode)
    print_success(
        "Presets and global skills backed up." if include_global else "Presets backed up."
    )


def commit_presets(message: str, include_config: bool = False, presets_only: bool = False) -> None:
    """Record local preset changes while keeping machine-specific files local."""
    keep_empty_presets()
    untrack_if_tracked("applied.json")
    untrack_if_tracked("cache")
    untrack_if_tracked(".metadata_cache.json")
    if include_config:
        if git(["add", "-f", "config.yaml"]).returncode != 0:
            sys.exit(1)
    else:
        untrack_if_tracked("config.yaml")
    if git(["add", "-A"]).returncode != 0:
        sys.exit(1)
    if presets_only:
        paths = git(["ls-files", "-z"], capture=True)
        if paths.returncode != 0:
            sys.exit(paths.returncode)
        for path in paths.stdout.split("\0"):
            if (
                path
                and path != ".gitignore"
                and not path.startswith("presets/")
                and not path.startswith("global/")
            ):
                if git(["rm", "--cached", "-q", "--", path]).returncode != 0:
                    sys.exit(1)

    staged = git(["diff", "--cached", "--quiet"]).returncode
    if staged == 0:
        print_warn("Nothing changed since the last backup.")
    elif staged != 1:
        sys.exit(staged)
    elif git(["commit", "-m", message]).returncode != 0:
        sys.exit(1)


def confirm_force_push(url: Optional[str], yes: bool) -> bool:
    if yes:
        return True
    print_warn(
        f"This overwrites {sanitize_url(url)} with what's here, permanently discarding any commits only it has."
    )
    if not sys.stdin.isatty():
        return False
    try:
        from rich.prompt import Confirm

        return Confirm.ask("Force-push anyway?", default=False, console=console)
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False


def explain_push_failure(stderr: str, url: str) -> None:
    """Name the actual cause: git's own hint is about the wrong one more often than not."""
    lowered = stderr.lower()
    name = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1] or DEFAULT_REPO_NAME

    if "not found" in lowered or "does not exist" in lowered:
        print_error(f"{sanitize_url(url)} does not exist, or your account cannot see it.")
        owner = github_login()
        if owner:
            console.print(
                f"Create it (private) with: [header]skctl backup init --repo {name}[/header]"
            )
            console.print(
                f"Your GitHub account is [bold]{owner}[/bold]; to point at an existing repo instead: "
                f"[header]skctl backup init --repo {owner}/{name}[/header]"
            )
        else:
            console.print(
                f"Create it on github.com, then: [header]skctl backup init --repo owner/{name}[/header]"
            )
    elif (
        "authentication" in lowered
        or "could not read username" in lowered
        or "permission denied" in lowered
    ):
        print_error("GitHub rejected your credentials.")
        console.print(
            "Log in with [header]gh auth login[/header], or use an SSH remote: "
            "[header]skctl backup init --repo git@github.com:owner/name.git[/header]"
        )
    elif "non-fast-forward" in lowered or "fetch first" in lowered or "rejected" in lowered:
        print_error("The backup has commits you do not have locally.")
        console.print("Run: [header]skctl backup pull[/header]")
    else:
        print_error("Push failed; see the git output above.")


def maybe_auto_push(what: str, background: bool = True) -> None:
    """Push after a change to the presets, when backup.auto_push is on.

    A backup failure must not fail the command the user actually ran, so this
    swallows the exit `backup_push` raises and only warns.
    """
    if not load_config().get("backup", {}).get("auto_push"):
        return
    if not has_repo() or not remote_url():
        return
    if background:
        try:
            subprocess.Popen(
                [sys.executable, "-m", "skill_ctl.cli", "backup", "push", "-m", what],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError:
            pass
    try:
        with backup_lock():
            backup_push(what, None, False)
    except SystemExit:
        print_warn("Auto-backup failed; run `skctl backup push` when you have a moment.")


def github_login() -> Optional[str]:
    """The logged-in gh account, when gh is available."""
    if shutil.which("gh") is None:
        return None
    result = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def backup_pull(
    repo: Optional[str],
    yes: bool,
    dry_run: bool = False,
    force: bool = False,
    include_global: bool = False,
) -> None:
    if dry_run:
        preview_backup_pull(repo)
        return
    if repo or not has_repo():
        backup_init(repo, False, yes)
    url = remote_url()
    if not url:
        print_error("No backup repository configured. Run: skctl backup init --repo owner/name")
        sys.exit(1)

    print_header(f"Fetching backup from {sanitize_url(url)}")
    if git(["fetch", "origin", BRANCH]).returncode != 0:
        sys.exit(1)

    before_entries = backed_up_global_entries() if include_global else set()

    # An empty local repo means a restore: there is no history to merge onto, and
    # whatever sits in ~/.skill-ctl now would block the checkout, so it is replaced.
    # --force does the same replace even when local has its own commits, discarding them.
    fresh = git(["rev-parse", "--verify", "HEAD"], capture=True).returncode != 0
    if fresh or force:
        if not yes:
            if not sys.stdin.isatty():
                print_error(
                    "Replacing local presets requires confirmation; pass --yes to approve without a terminal."
                )
                sys.exit(1)
            from rich.prompt import Confirm

            console.print()
            prompt = (
                f"Replace the contents of {BASE_DIR} with the backup?"
                if fresh
                else f"Discard local commits not in the backup and replace {BASE_DIR} with it?"
            )
            try:
                confirmed = Confirm.ask(f"[bold]{prompt}[/bold]", default=False, console=console)
            except (EOFError, KeyboardInterrupt):
                console.print()
                sys.exit(0)
            if not confirmed:
                sys.exit(0)
        if git(["checkout", "-f", "-B", BRANCH, f"origin/{BRANCH}"]).returncode != 0:
            sys.exit(1)
        print_success(f"Restored presets into {BASE_DIR}")
        if include_global:
            after_entries = backed_up_global_entries()
            restore_global_skills(removed_entries=before_entries - after_entries, force=force)
        return

    if git(["merge", "--ff-only", f"origin/{BRANCH}"]).returncode != 0:
        print_error("Local presets have changes the backup does not.")
        console.print(
            "  [header]skctl backup push --force[/header]  (send your local changes up, overwriting the backup)"
        )
        console.print(
            "  [header]skctl backup pull --force[/header]  (discard your local changes, take the backup's instead)"
        )
        console.print(f"  [header]cd {BASE_DIR} && git status[/header]  (or merge by hand)")
        sys.exit(1)
    print_success("Presets up to date with the backup.")
    if include_global:
        after_entries = backed_up_global_entries()
        restore_global_skills(removed_entries=before_entries - after_entries, force=force)


def preview_backup_pull(repo: Optional[str] = None) -> None:
    """Fetch and compare in a temporary repo, preserving all local Git state."""
    if shutil.which("git") is None:
        print_error("'git' not found on PATH. Install git and retry.")
        sys.exit(1)
    url = repo_url(repo) if repo else remote_url()
    if not url:
        print_error("No backup repository configured. Pass --repo owner/name to preview a backup.")
        sys.exit(1)
    print_header(f"Fetching backup preview from {sanitize_url(url)}")
    with tempfile.TemporaryDirectory(prefix="skctl-restore-preview-") as directory:

        def preview_git(args):
            result = subprocess.run(
                ["git", "-C", directory, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                console.print((result.stderr or result.stdout).rstrip(), markup=False)
                sys.exit(result.returncode)
            return result

        preview_git(["init", "--bare", "-q"])
        preview_git(["fetch", "--quiet", "--no-tags", "--", url, f"{BRANCH}:refs/heads/incoming"])
        head = git(["rev-parse", "--verify", "HEAD"], capture=True) if has_repo() else None
        if head is not None and head.returncode == 0:
            preview_git(["fetch", "--quiet", "--no-tags", str(BASE_DIR), "HEAD:refs/heads/local"])
            output = preview_git(["diff", "--name-status", "local", "incoming"]).stdout
            lines = [line for line in output.splitlines() if line.strip()]
        else:
            output = preview_git(["ls-tree", "-r", "--name-only", "incoming"]).stdout
            lines = [f"A\t{path}" for path in output.splitlines() if path.strip()]
    if not lines:
        print_success("Restore preview: no file changes.")
        return
    print_header("Restore preview")
    for line in lines:
        status, path = line.split("\t", 1)
        label = {"A": "add", "D": "remove", "M": "change"}.get(status[:1], status)
        console.print(f"  {label:6} {path}")
    print_warn(f"{len(lines)} file change(s); run `skctl backup pull` to apply them.")


def backup_status(include_global: bool = False) -> None:
    if not has_repo():
        print_warn(f"{BASE_DIR} is not backed up yet.")
        console.print("Start with: [header]skctl backup init --repo owner/name[/header]")
        return

    url = remote_url()
    print_header(f"Backup repository: {url or '(none configured)'}")
    dirty = git(["status", "--short"], capture=True).stdout.strip()
    global_changes = uncommitted_global_changes() if include_global else []
    if dirty or global_changes:
        dirty_lines = dirty.splitlines() if dirty else []
        changed = len(dirty_lines) + len(global_changes)
        print_warn(f"{changed} uncommitted change{'s' if changed != 1 else ''} in {BASE_DIR}")
        combined = dirty_lines + [f" {line}" for line in global_changes]
        console.print("\n".join(combined[:20] + (["  ..."] if len(combined) > 20 else [])))
    else:
        print_success("Everything is committed.")

    if url:
        counts = git(
            ["rev-list", "--left-right", "--count", f"origin/{BRANCH}...HEAD"], capture=True
        )
        if counts.returncode == 0 and counts.stdout.split():
            behind, ahead = counts.stdout.split()
            if ahead != "0":
                print_warn(f"{ahead} commit(s) not pushed yet. Run: skctl backup push")
            if behind != "0":
                print_warn(
                    f"{behind} commit(s) on the backup you do not have. Run: skctl backup pull"
                )
            if ahead == behind == "0" and not dirty and not global_changes:
                print_success("Backup is current.")
