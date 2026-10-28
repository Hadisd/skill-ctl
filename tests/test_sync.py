"""Exercise two-device sync using real Git repositories and isolated homes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def device(tmp_path, name):
    home = tmp_path / name
    home.mkdir()
    return dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=str(REPO_ROOT),
        GIT_AUTHOR_NAME="sync test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="sync test",
        GIT_COMMITTER_EMAIL="test@example.com",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
    )


def base(env):
    return Path(env["HOME"]) / ".skill-ctl"


def skill(env, name="tdd", content="# tdd\n"):
    path = base(env) / "presets" / "demo" / ".agents" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def git(env, *args):
    return subprocess.run(
        ["git", "-C", str(base(env)), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def run(env, *args, ok=True):
    result = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", *args],
        env=env,
        cwd=env["HOME"],
        capture_output=True,
        text=True,
    )
    if ok:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
    return result.stdout + result.stderr


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(path)], check=True)
    return f"file://{path}"


def pair(tmp_path, remote):
    first, second = device(tmp_path, "first"), device(tmp_path, "second")
    skill(first)
    for env in (first, second):
        run(env, "backup", "init", "--repo", remote)
        run(env, "sync")
    # Both devices share history before editing independently.
    run(first, "sync")
    return first, second


def test_sync_initial_upload_fresh_restore_and_idempotence(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    assert (base(second) / "presets/demo/.agents/skills/tdd/SKILL.md").read_text() == "# tdd\n"
    head = git(first, "rev-parse", "HEAD")
    assert "Presets synced" in run(first, "sync")
    assert git(first, "rev-parse", "HEAD") == head
    assert git(first, "status", "--porcelain") == ""


def test_sync_merges_independent_edits_and_deletions(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    skill(first, "react", "# react\n")
    skill(second, "pytest", "# pytest\n")
    (base(second) / "presets/demo/.agents/skills/tdd/SKILL.md").unlink()
    run(first, "sync")
    run(second, "sync", "-m", "desktop changes")
    run(first, "sync")
    for env in (first, second):
        assert (base(env) / "presets/demo/.agents/skills/react/SKILL.md").read_text() == "# react\n"
        assert (
            base(env) / "presets/demo/.agents/skills/pytest/SKILL.md"
        ).read_text() == "# pytest\n"
        assert not (base(env) / "presets/demo/.agents/skills/tdd/SKILL.md").exists()
        assert git(env, "status", "--porcelain") == ""
    assert git(first, "rev-parse", "HEAD") == git(second, "rev-parse", "HEAD")
    assert "desktop changes" in git(second, "log", "--format=%s")


def test_sync_handles_utf8_paths_with_windows_default_encoding(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    name = "\u540d-skill"
    skill(first, name, "# Unicode filename\n")
    run(first, "sync")
    skill(second, "local-skill", "# local work\n")
    # Exercise the real Git subprocesses with Windows' CP1252 text default.
    # The UTF-8 filename includes 0x8d, which CP1252 cannot decode.
    bootstrap = (
        "import subprocess; subprocess._text_encoding = lambda: 'cp1252'; "
        "from skill_ctl.cli import main; main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, "sync"],
        env=second,
        cwd=second["HOME"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Presets synced" in result.stdout
    assert (
        base(second) / f"presets/demo/.agents/skills/{name}/SKILL.md"
    ).read_text() == "# Unicode filename\n"
    run(first, "sync")
    assert (
        base(first) / "presets/demo/.agents/skills/local-skill/SKILL.md"
    ).read_text() == "# local work\n"


def test_git_capture_handles_invalid_utf8_output(tmp_path, remote, monkeypatch):
    import skill_ctl.backup as backup

    first, _ = pair(tmp_path, remote)
    content = base(first) / "presets/demo/.agents/skills/tdd/SKILL.md"
    content.write_bytes(b"# title\ninvalid byte: \xff\n")
    git(first, "add", "presets")
    git(first, "commit", "-m", "non-UTF8 skill content")
    monkeypatch.setattr(backup, "BASE_DIR", base(first))
    result = backup.git(["show", "HEAD:presets/demo/.agents/skills/tdd/SKILL.md"], capture=True)
    assert result.returncode == 0
    assert result.stdout == "# title\ninvalid byte: \ufffd\n"


def test_sync_conflict_aborts_preserves_versions_and_can_be_resolved(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    skill(first, content="# laptop version\n")
    local = skill(second, content="# desktop version\n")
    run(first, "sync")
    remote_head = git(first, "rev-parse", "HEAD")
    output = run(second, "sync", ok=False)
    assert "Conflicting files" in output
    assert "merge was aborted" in output
    assert local.read_text() == "# desktop version\n"
    assert git(second, "status", "--porcelain") == ""
    assert (
        git(second, "show", "HEAD:presets/demo/.agents/skills/tdd/SKILL.md") == "# desktop version"
    )
    assert (
        git(second, "show", "origin/main:presets/demo/.agents/skills/tdd/SKILL.md")
        == "# laptop version"
    )
    assert not (base(second) / ".git/MERGE_HEAD").exists()
    assert git(second, "ls-remote", "origin", "refs/heads/main").split()[0] == remote_head

    # Retrying cannot silently choose a side or overwrite the backup.
    run(second, "sync", ok=False)
    merge = subprocess.run(
        ["git", "-C", str(base(second)), "merge", "--no-edit", "origin/main"],
        env=second,
        capture_output=True,
        text=True,
    )
    assert merge.returncode != 0
    run(second, "sync", ok=False)  # An unresolved manual merge is protected.
    local.write_text("# resolved version\n")
    git(second, "add", "presets")
    git(second, "commit", "-m", "resolve skill conflict")
    run(second, "sync")
    run(first, "sync")
    assert (
        base(first) / "presets/demo/.agents/skills/tdd/SKILL.md"
    ).read_text() == "# resolved version\n"


@pytest.mark.parametrize("args", [("sync", "--dry-run"), ("sync", "status")])
def test_preview_and_status_fetch_without_changing_contents_or_history(tmp_path, remote, args):
    first, second = pair(tmp_path, remote)
    skill(first, "react")
    run(first, "sync")
    skill(second, "pytest")
    git(second, "add", "presets")
    git(second, "commit", "-m", "local skill")
    local = skill(second, "pending", "# not committed\n")
    config = base(second) / "config.yaml"
    config.unlink()  # A read-only operation must not create or migrate it.
    head = git(second, "rev-parse", "HEAD")
    remote_head = git(second, "ls-remote", "origin", "refs/heads/main")
    dirty = git(second, "status", "--porcelain")
    output = run(second, *args)
    assert "1 incoming commit(s), 1 outgoing commit(s)" in output
    assert "react" in output and "pytest" in output and "pending" in output
    assert local.read_text() == "# not committed\n"
    assert not config.exists()
    assert not (base(second) / "presets/demo/.agents/skills/react").exists()
    assert git(second, "rev-parse", "HEAD") == head
    assert git(second, "ls-remote", "origin", "refs/heads/main") == remote_head
    assert git(second, "status", "--porcelain") == dirty


def test_sync_on_fresh_device_merges_existing_local_skills(tmp_path, remote):
    first = device(tmp_path, "first")
    skill(first, "react")
    run(first, "backup", "init", "--repo", remote)
    run(first, "sync")
    second = device(tmp_path, "second")
    skill(second, "pytest")
    run(second, "backup", "init", "--repo", remote)
    run(second, "sync")
    assert (base(second) / "presets/demo/.agents/skills/react/SKILL.md").exists()
    assert (base(second) / "presets/demo/.agents/skills/pytest/SKILL.md").exists()
    run(first, "sync")
    assert (base(first) / "presets/demo/.agents/skills/pytest/SKILL.md").exists()


def test_sync_keeps_machine_files_local_and_preserves_empty_presets(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    config = base(second) / "config.yaml"
    config.write_text("theme: light\n")
    (base(first) / "applied.json").write_text('{"projects": {}}')
    cache = base(first) / "cache"
    cache.mkdir()
    (cache / "private.json").write_text("{}")
    (base(first) / "presets/empty").mkdir()
    run(first, "sync")
    run(second, "sync")
    assert "theme: light" in config.read_text()
    assert not (base(second) / "applied.json").exists()
    assert not (base(second) / "cache/private.json").exists()
    assert (base(second) / "presets/empty").is_dir()
    assert git(first, "ls-files", "config.yaml", "applied.json", "cache", ".backup.lock") == ""


def test_sync_refuses_remote_machine_files(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    run(first, "backup", "push", "--config")
    config = base(second) / "config.yaml"
    before = config.read_bytes()
    head = git(second, "rev-parse", "HEAD")
    output = run(second, "sync", ok=False)
    assert "files outside presets" in output
    assert config.read_bytes() == before
    assert git(second, "rev-parse", "HEAD") == head


def test_sync_requires_configuration(tmp_path):
    env = device(tmp_path, "first")
    assert "backup init --repo" in run(env, "sync", "--dry-run", ok=False)
    assert not base(env).exists()


def test_sync_fetch_failure_leaves_uncommitted_skills_untouched(tmp_path, remote):
    first, _ = pair(tmp_path, remote)
    local = skill(first, content="# pending\n")
    head = git(first, "rev-parse", "HEAD")
    git(first, "remote", "set-url", "origin", f"file://{tmp_path}/missing.git")
    run(first, "sync", ok=False)
    assert local.read_text() == "# pending\n"
    assert git(first, "rev-parse", "HEAD") == head


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX lock verification")
def test_backup_and_sync_share_an_operation_lock(tmp_path, remote):
    import fcntl

    first, _ = pair(tmp_path, remote)
    with (base(first) / ".backup.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for args in (("sync",), ("backup", "push"), ("backup", "pull")):
            output = run(first, *args, ok=False)
            assert "Another backup or sync is running" in output


@pytest.mark.parametrize("shell", ["fish", "bash", "zsh", "powershell"])
def test_sync_is_in_completion_scripts(tmp_path, shell):
    output = run(device(tmp_path, "first"), "completion", shell)
    assert "sync" in output
    if shell == "fish":
        assert "-l dry-run" in output and "-l message" in output
    else:
        assert "--dry-run" in output and "--message" in output


def global_skill(env, name="caveman", content="# caveman\n"):
    path = Path(env["HOME"]) / ".agents" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_sync_global_skills_two_way_add_merge_and_delete(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    global_skill(first, "caveman", "# caveman\n")
    global_skill(second, "humanizer", "# humanizer\n")

    # Device 1 and Device 2 independently sync with -g
    run(first, "sync", "-g")
    run(second, "sync", "--global")
    run(first, "sync", "-g")

    for env in (first, second):
        home = Path(env["HOME"])
        assert (home / ".agents/skills/caveman/SKILL.md").read_text(
            encoding="utf-8"
        ) == "# caveman\n"
        assert (home / ".agents/skills/humanizer/SKILL.md").read_text(
            encoding="utf-8"
        ) == "# humanizer\n"

    # Deleting a global skill on Device 2 and syncing removes it on Device 1 too
    import shutil

    shutil.rmtree(Path(second["HOME"]) / ".agents" / "skills" / "caveman")
    run(second, "sync", "-g")
    run(first, "sync", "-g")
    assert not (Path(first["HOME"]) / ".agents" / "skills" / "caveman").exists()
    assert (Path(first["HOME"]) / ".agents" / "skills" / "humanizer" / "SKILL.md").is_file()


def test_sync_global_dry_run_and_status_do_not_modify_disk(tmp_path, remote):
    first, _ = pair(tmp_path, remote)
    global_skill(first, "pending-global", "# pending\n")
    head = git(first, "rev-parse", "HEAD")

    for args in (("sync", "--dry-run", "-g"), ("sync", "status", "-g")):
        output = run(first, *args)
        assert "global/.agents/skills/pending-global" in output
        assert not (base(first) / "global").exists()
        assert not (base(first) / "global-sync.json").exists()
        assert git(first, "rev-parse", "HEAD") == head


def put_global(env, rel_dir, name):
    path = Path(env["HOME"]) / rel_dir / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n", encoding="utf-8")


def test_sync_global_restores_agent_folder_missing_on_other_device(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    put_global(first, ".cursor/skills", "cur")
    run(first, "sync", "-g")
    run(second, "sync", "-g")
    run(second, "sync", "-g")
    run(first, "sync", "-g")
    assert (Path(first["HOME"]) / ".cursor/skills/cur/SKILL.md").is_file()
    assert (Path(second["HOME"]) / ".cursor/skills/cur/SKILL.md").is_file()


def test_sync_global_narrower_config_does_not_delete_other_device_skills(tmp_path, remote):
    first, second = pair(tmp_path, remote)
    put_global(first, ".cursor/skills", "cur")
    run(first, "sync", "-g")
    cfg = base(second) / "config.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8") + '\nglobal_skill_dirs:\n  - "~/.agents/skills"\n',
        encoding="utf-8",
    )
    run(second, "sync", "-g")
    run(second, "sync", "-g")
    run(first, "sync", "-g")
    assert (Path(first["HOME"]) / ".cursor/skills/cur/SKILL.md").is_file()
    assert not (Path(second["HOME"]) / ".cursor").exists()
    assert "global/.cursor" not in run(second, "sync", "status", "-g")

    cfg.write_text(cfg.read_text(encoding="utf-8") + '  - "~/.cursor/skills"\n', encoding="utf-8")
    run(second, "sync", "-g")
    assert (Path(second["HOME"]) / ".cursor/skills/cur/SKILL.md").is_file()


@pytest.mark.parametrize("download", [("sync",), ("backup", "pull", "-y")])
def test_enabling_global_sync_preserves_skills_downloaded_without_restoring(
    tmp_path, remote, download
):
    first, second = pair(tmp_path, remote)
    global_skill(first, "remote-only", "# Remote\n")
    run(first, "sync", "-g")
    run(second, *download)
    global_skill(second, "local-only", "# Local\n")
    assert "D\tglobal/.agents/skills/remote-only" not in run(second, "sync", "status", "-g")

    run(second, "sync", "-g")
    run(first, "sync", "-g")
    for env in (first, second):
        assert (
            Path(env["HOME"]) / ".agents/skills/remote-only/SKILL.md"
        ).read_text() == "# Remote\n"
        assert (Path(env["HOME"]) / ".agents/skills/local-only/SKILL.md").read_text() == "# Local\n"
        assert (base(env) / "global-sync.json").is_file()
        assert git(env, "ls-files", "global-sync.json") == ""


def test_global_sync_after_plain_sync_restores_remote_edits_and_deletions(tmp_path, remote):
    import shutil

    first, second = pair(tmp_path, remote)
    global_skill(first, "shared", "# Original\n")
    global_skill(first, "deleted", "# Delete me\n")
    lock = Path(first["HOME"]) / ".agents/.skill-lock.json"
    lock.write_text(json.dumps({"version": 1, "skills": {"shared": {"source": "old/repo"}}}))
    run(first, "sync", "-g")
    run(second, "sync", "-g")
    global_skill(first, "shared", "# Updated remotely\n")
    lock.write_text(json.dumps({"version": 1, "skills": {"shared": {"source": "new/repo"}}}))
    shutil.rmtree(Path(first["HOME"]) / ".agents/skills/deleted")
    run(first, "sync", "-g")
    run(second, "sync")
    run(second, "sync", "-g")
    run(first, "sync", "-g")
    for env in (first, second):
        assert (
            Path(env["HOME"]) / ".agents/skills/shared/SKILL.md"
        ).read_text() == "# Updated remotely\n"
        assert not (Path(env["HOME"]) / ".agents/skills/deleted").exists()
        metadata = json.loads((Path(env["HOME"]) / ".agents/.skill-lock.json").read_text())
        assert metadata["skills"]["shared"]["source"] == "new/repo"


def test_global_sync_preserves_both_versions_when_plain_sync_downloaded_conflicting_edits(
    tmp_path, remote
):
    first, second = pair(tmp_path, remote)
    global_skill(first, "shared", "# Original\n")
    run(first, "sync", "-g")
    run(second, "sync", "-g")
    global_skill(first, "shared", "# Remote edit\n")
    run(first, "sync", "-g")
    global_skill(second, "shared", "# Local edit\n")
    global_skill(second, "aaa-local", "# Pending local skill\n")
    run(second, "sync")
    head = git(second, "rev-parse", "HEAD")

    output = run(second, "sync", "-g", ok=False)
    assert "changed both locally and in the backup" in output
    assert git(second, "rev-parse", "HEAD") == head
    assert (Path(second["HOME"]) / ".agents/skills/shared/SKILL.md").read_text() == "# Local edit\n"
    assert (base(second) / "global/.agents/skills/shared/SKILL.md").read_text() == "# Remote edit\n"
    assert not (base(second) / "global/.agents/skills/aaa-local").exists()


def test_global_sync_state_cannot_delete_parent_paths(tmp_path, remote):
    first, _ = pair(tmp_path, remote)
    home = Path(first["HOME"])
    (home / ".agents/skills").mkdir(parents=True)
    private = home / ".agents/private/keep.txt"
    private.parent.mkdir()
    private.write_text("Keep this file\n", encoding="utf-8")
    (base(first) / "global-sync.json").write_text(
        json.dumps({".agents/skills": {"../private": "invalid"}}), encoding="utf-8"
    )
    run(first, "sync", "-g")
    assert private.read_text() == "Keep this file\n"
