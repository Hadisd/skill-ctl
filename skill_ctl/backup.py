"""Backing ~/.skill-ctl up to a GitHub repository (backup init/push/pull/status)."""

import shutil
import subprocess
import sys
from typing import Annotated, Literal, Optional

import typer

from skill_ctl.constants import BASE_DIR
from skill_ctl.config import ensure_config, load_config
from skill_ctl.ui import console, print_header, print_success, print_warn, print_error

BRANCH = "main"
DEFAULT_REPO_NAME = "skill-ctl-presets"

GITIGNORE = """# Written by `skctl backup`. Everything else here is the backup.
node_modules/
__pycache__/
*.pyc
.DS_Store
.metadata_cache.json
"""



def git(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    """Run git inside ~/.skill-ctl."""
    if shutil.which("git") is None:
        print_error("'git' not found on PATH. Install git and retry.")
        sys.exit(1)
    return subprocess.run(
        ["git", "-C", str(BASE_DIR)] + args,
        capture_output=capture,
        text=True,
    )


def has_repo() -> bool:
    return (BASE_DIR / ".git").is_dir()


def remote_url() -> Optional[str]:
    if not has_repo():
        return None
    result = git(["remote", "get-url", "origin"], capture=True)
    return result.stdout.strip() if result.returncode == 0 else None


def repo_url(repo: str) -> str:
    """Accept a full URL, 'owner/name', or a bare name (which needs gh to create)."""
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
    if not gitignore.exists():
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
        capture_output=True, text=True,
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
        typer.Option("--repo", "-r", help="Backup repository: owner/name, a full git URL, or a bare name to create with gh.")
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option("--message", "-m", help="Commit message for 'push' (default: 'backup presets').")
    ] = None,
    public: Annotated[
        bool,
        typer.Option("--public", help="Create the gh repo public instead of private.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation when a pull overwrites local presets.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show incoming backup changes without restoring them.")
    ] = False,
) -> None:
    """Back up ~/.skill-ctl (presets and config.yaml) to a GitHub repository.

    `init` points it at a repo, `push` commits and uploads, `pull` restores on
    another machine, `status` shows what is not backed up yet.
    """
    if action == "init":
        backup_init(repo, public)
    elif action == "push":
        backup_push(message, repo, public)
    elif action == "pull":
        backup_pull(repo, yes, dry_run)
    else:
        backup_status()


def backup_init(repo: Optional[str], public: bool) -> None:
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
            print_warn("Run: skctl backup init --repo owner/name   (or install the gh CLI to create one)")
            sys.exit(1)

    print_success(f"Backing up {BASE_DIR} to {url}")
    print_warn("Presets are uploaded as-is; use a private repo if any skill is not public.")
    console.print("Next: [header]skctl backup push[/header]")


def backup_push(message: Optional[str], repo: Optional[str], public: bool) -> None:
    # backup_init calls ensure_local_repo itself, so only the already-set-up
    # path needs it here.
    if not has_repo() or not remote_url():
        backup_init(repo, public)
    else:
        ensure_local_repo()

    keep_empty_presets()
    if git(["add", "-A"]).returncode != 0:
        sys.exit(1)

    staged = git(["diff", "--cached", "--quiet"]).returncode
    if staged == 0:
        print_warn("Nothing changed since the last backup.")
    elif git(["commit", "-m", message or "backup presets"]).returncode != 0:
        sys.exit(1)

    url = remote_url()
    print_header(f"Pushing {BASE_DIR} to {url}")
    result = git(["push", "-u", "origin", BRANCH], capture=True)
    if result.returncode != 0:
        console.print((result.stderr or result.stdout).rstrip())
        explain_push_failure(result.stderr or "", url or "")
        sys.exit(result.returncode)
    print_success("Presets backed up.")


def explain_push_failure(stderr: str, url: str) -> None:
    """Name the actual cause: git's own hint is about the wrong one more often than not."""
    lowered = stderr.lower()
    name = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1] or DEFAULT_REPO_NAME

    if "not found" in lowered or "does not exist" in lowered:
        print_error(f"{url} does not exist, or your account cannot see it.")
        owner = github_login()
        if owner:
            console.print(f"Create it (private) with: [header]skctl backup init --repo {name}[/header]")
            console.print(f"Your GitHub account is [bold]{owner}[/bold]; to point at an existing repo instead: "
                          f"[header]skctl backup init --repo {owner}/{name}[/header]")
        else:
            console.print(f"Create it on github.com, then: [header]skctl backup init --repo owner/{name}[/header]")
    elif "authentication" in lowered or "could not read username" in lowered or "permission denied" in lowered:
        print_error("GitHub rejected your credentials.")
        console.print("Log in with [header]gh auth login[/header], or use an SSH remote: "
                      "[header]skctl backup init --repo git@github.com:owner/name.git[/header]")
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
        backup_push(what, None, False)
    except SystemExit:
        print_warn("Auto-backup failed; run `skctl backup push` when you have a moment.")



def github_login() -> Optional[str]:
    """The logged-in gh account, when gh is available."""
    if shutil.which("gh") is None:
        return None
    result = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def backup_pull(repo: Optional[str], yes: bool, dry_run: bool = False) -> None:
    if repo or not has_repo():
        backup_init(repo, False)
    url = remote_url()
    if not url:
        print_error("No backup repository configured. Run: skctl backup init --repo owner/name")
        sys.exit(1)

    print_header(f"Fetching backup from {url}")
    if git(["fetch", "origin", BRANCH]).returncode != 0:
        sys.exit(1)

    if dry_run:
        preview_backup_pull()
        return

    # An empty local repo means a restore: there is no history to merge onto, and
    # whatever sits in ~/.skill-ctl now would block the checkout, so it is replaced.
    if git(["rev-parse", "--verify", "HEAD"], capture=True).returncode != 0:
        if not yes and sys.stdin.isatty():
            from rich.prompt import Confirm
            console.print()
            if not Confirm.ask(
                f"[bold]Replace the contents of {BASE_DIR} with the backup?[/bold]",
                default=False, console=console,
            ):
                sys.exit(0)
        if git(["checkout", "-f", "-B", BRANCH, f"origin/{BRANCH}"]).returncode != 0:
            sys.exit(1)
        print_success(f"Restored presets into {BASE_DIR}")
        return

    if git(["merge", "--ff-only", f"origin/{BRANCH}"]).returncode != 0:
        print_error("Local presets have changes the backup does not. Push them first, or merge by hand:")
        console.print(f"  [header]cd {BASE_DIR} && git status[/header]")
        sys.exit(1)
    print_success("Presets up to date with the backup.")


def preview_backup_pull() -> None:
    """Show the files a restore would add, remove, or change without writing."""
    head = git(["rev-parse", "--verify", "HEAD"], capture=True)
    if head.returncode == 0:
        result = git(["diff", "--name-status", "HEAD", f"origin/{BRANCH}"], capture=True)
    else:
        result = git(["diff-tree", "--root", "--no-commit-id", "--name-status", "-r", f"origin/{BRANCH}"], capture=True)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        print_success("Restore preview: no file changes.")
        return
    print_header("Restore preview")
    for line in lines:
        status, path = line.split("\t", 1)
        label = {"A": "add", "D": "remove", "M": "change"}.get(status[:1], status)
        console.print(f"  {label:6} {path}")
    print_warn(f"{len(lines)} file change(s); run `skctl backup pull` to apply them.")


def backup_status() -> None:
    if not has_repo():
        print_warn(f"{BASE_DIR} is not backed up yet.")
        console.print("Start with: [header]skctl backup init --repo owner/name[/header]")
        return

    url = remote_url()
    print_header(f"Backup repository: {url or '(none configured)'}")
    dirty = git(["status", "--short"], capture=True).stdout.strip()
    if dirty:
        changed = len(dirty.splitlines())
        print_warn(f"{changed} uncommitted change{'s' if changed != 1 else ''} in {BASE_DIR}")
        console.print(dirty if changed <= 20 else "\n".join(dirty.splitlines()[:20] + ["  ..."]))
    else:
        print_success("Everything is committed.")

    if url:
        counts = git(["rev-list", "--left-right", "--count", f"origin/{BRANCH}...HEAD"], capture=True)
        if counts.returncode == 0 and counts.stdout.split():
            behind, ahead = counts.stdout.split()
            if ahead != "0":
                print_warn(f"{ahead} commit(s) not pushed yet. Run: skctl backup push")
            if behind != "0":
                print_warn(f"{behind} commit(s) on the backup you do not have. Run: skctl backup pull")
            if ahead == behind == "0" and not dirty:
                print_success("Backup is current.")
