"""Two-way preset synchronization through the configured backup repository."""

from pathlib import Path
from typing import Annotated, Literal, Optional

import typer

from skill_ctl.backup import (
    BRANCH, backup_lock, commit_presets, ensure_local_repo, git, has_repo, remote_url,
)
from skill_ctl.constants import BASE_DIR
from skill_ctl.ui import console, print_error, print_header, print_success, print_warn

REMOTE_BRANCH = f"origin/{BRANCH}"


def checked_git(args: list[str]):
    result = git(args, capture=True)
    if result.returncode != 0:
        console.print((result.stderr or result.stdout).rstrip(), markup=False)
        raise SystemExit(1)
    return result


def has_commit(ref: str) -> bool:
    return git(["rev-parse", "--verify", "--quiet", ref], capture=True).returncode == 0


def check_sync_state() -> None:
    branch = checked_git(["symbolic-ref", "--short", "HEAD"]).stdout.strip()
    if branch != BRANCH:
        print_error(f"Sync requires the '{BRANCH}' branch; the backup is on '{branch}'.")
        raise SystemExit(1)
    git_dir = Path(checked_git(["rev-parse", "--absolute-git-dir"]).stdout.strip())
    pending = any((git_dir / name).exists() for name in (
        "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer",
    ))
    unmerged = checked_git(["diff", "--name-only", "--diff-filter=U"]).stdout.strip()
    if pending or unmerged:
        print_error("Finish or abort the Git operation in the backup repository before syncing.")
        console.print(f"Run git status in {BASE_DIR}", markup=False)
        raise SystemExit(1)


def check_remote_files() -> None:
    """Never bring a legacy backup's machine-specific files onto this device."""
    paths = checked_git(["ls-tree", "-rz", "--name-only", REMOTE_BRANCH]).stdout.split("\0")
    excluded = [path for path in paths if path and path != ".gitignore" and not path.startswith("presets/")]
    if excluded:
        print_error("The backup contains files outside presets; sync keeps machine-specific files local.")
        for path in excluded:
            console.print(f"  {path}", markup=False)
        print_warn("Remove those files from the backup, or use backup pull to restore it explicitly.")
        raise SystemExit(1)


def show_sync_status(remote_exists: bool, preview: bool = False) -> None:
    print_header("Sync preview" if preview else "Sync status")
    dirty = checked_git(["status", "--short", "--", "presets", ".gitignore"]).stdout.strip()
    if dirty:
        console.print("Local changes to commit:")
        console.print(dirty, markup=False)

    local_exists = has_commit("HEAD")
    if not remote_exists:
        console.print("The remote has no main branch yet; sync will upload local presets.")
        return
    if not local_exists:
        console.print("Sync will bring in the remote presets and merge any local presets.")
        incoming = checked_git(["ls-tree", "-r", "--name-only", REMOTE_BRANCH]).stdout
    else:
        behind, ahead = checked_git([
            "rev-list", "--left-right", "--count", f"{REMOTE_BRANCH}...HEAD",
        ]).stdout.split()
        console.print(f"{behind} incoming commit(s), {ahead} outgoing commit(s).")
        if behind == ahead == "0" and not dirty:
            print_success("Presets are in sync.")
            return
        base = git(["merge-base", "HEAD", REMOTE_BRANCH], capture=True)
        if base.returncode not in (0, 1):
            console.print(base.stderr.rstrip(), markup=False)
            raise SystemExit(1)
        incoming = checked_git([
            "diff", "--name-status", base.stdout.strip() or "HEAD", REMOTE_BRANCH, "--", "presets",
        ]).stdout if behind != "0" else ""
        outgoing = checked_git([
            "diff", "--name-status", base.stdout.strip() or REMOTE_BRANCH, "HEAD", "--", "presets",
        ]).stdout if ahead != "0" else ""
        if outgoing.strip():
            console.print("Outgoing preset changes:")
            console.print(outgoing.rstrip(), markup=False)
    if incoming.strip():
        console.print("Incoming files:")
        console.print(incoming.rstrip(), markup=False)
    if preview:
        print_warn("Preview only. No presets, commits, or remote files changed; a merge may need conflict resolution.")


def merge_remote() -> None:
    result = git([
        "merge", "--no-edit", "--no-stat", "--allow-unrelated-histories", REMOTE_BRANCH,
    ], capture=True)
    if result.returncode == 0:
        return
    conflicts = git(["diff", "--name-only", "--diff-filter=U"], capture=True).stdout.strip()
    if has_commit("MERGE_HEAD"):
        checked_git(["merge", "--abort"])
    print_error("Sync stopped; the remote was not changed.")
    if conflicts:
        console.print("Conflicting files:")
        console.print(conflicts, markup=False)
        print_warn("The merge was aborted. Local changes remain committed; remote versions remain in origin/main.")
        console.print(f"Resolve in {BASE_DIR}:", markup=False)
        console.print("  git merge --no-edit --allow-unrelated-histories origin/main", markup=False)
        console.print("  Edit the conflicts, then git add the resolved files and git commit.")
        console.print("  Run skctl sync again.")
    else:
        console.print((result.stderr or result.stdout).rstrip(), markup=False)
    raise SystemExit(1)


def sync(
    action: Annotated[Literal["run", "status"], typer.Argument()] = "run",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Fetch and preview incoming and outgoing changes without merging or pushing."),
    ] = False,
    message: Annotated[
        Optional[str], typer.Option("--message", "-m", help="Commit message for local changes (default: sync presets)."),
    ] = None,
) -> None:
    """Sync presets between devices using the configured Git backup.

    Run backup init --repo owner/name on each device first. Changes that do not
    conflict are merged and pushed; conflicts preserve both versions in Git.
    Configuration, caches, and this device's project paths stay local.
    """
    if not has_repo() or not remote_url():
        print_error("No backup repository configured. Run: skctl backup init --repo owner/name")
        raise SystemExit(1)
    with backup_lock():
        check_sync_state()
        print_header(f"Fetching backup from {remote_url()}")
        checked_git(["fetch", "--prune", "origin"])
        remote_exists = has_commit(f"refs/remotes/{REMOTE_BRANCH}")
        if remote_exists:
            check_remote_files()
        if dry_run or action == "status":
            show_sync_status(remote_exists, preview=dry_run)
            return

        ensure_local_repo()
        commit_presets(message or "sync presets", presets_only=True)
        if remote_exists:
            merge_remote()
        result = git(["push", "-u", "origin", BRANCH], capture=True)
        if result.returncode != 0:
            console.print((result.stderr or result.stdout).rstrip(), markup=False)
            print_error("Push failed. Local commits are saved; run skctl sync again to fetch and retry.")
            raise SystemExit(1)
        print_success("Presets synced.")
