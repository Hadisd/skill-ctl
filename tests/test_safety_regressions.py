"""Regression tests for destructive CLI operations, with isolated homes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    source = home / ".skill-ctl" / "presets" / "demo" / "a"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("original preset\n", encoding="utf-8")
    env = dict(os.environ, HOME=str(home), PYTHONPATH=str(REPO_ROOT),
               GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "skill_ctl.cli", *args], cwd=project,
            env=env, input="", capture_output=True, text=True,
        )

    return home, project, source, run


@pytest.mark.parametrize("first_agent", [None, "universal"])
def test_reapplying_copies_preserves_unowned_target(sandbox, first_agent):
    home, project, source, run = sandbox
    custom = project / ".claude" / "skills" / "a"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text("unrelated user work\n", encoding="utf-8")
    args = ["apply", "demo", "--copy"]
    if first_agent:
        args.extend(["--agent", first_agent])
    first = run(*args)
    assert first.returncode == 0, first.stderr
    (source / "SKILL.md").write_text("updated preset\n", encoding="utf-8")
    second = run("apply", "demo", "--copy")
    assert second.returncode == 0, second.stderr
    assert (custom / "SKILL.md").read_text() == "unrelated user work\n"
    assert (project / ".agents" / "skills" / "a" / "SKILL.md").read_text() == "updated preset\n"


def test_legacy_copy_record_does_not_authorize_overwriting(sandbox):
    home, project, source, run = sandbox
    assert run("apply", "demo", "--copy").returncode == 0
    record_path = project / "skills-applied.json"
    record = json.loads(record_path.read_text())
    record["presets"]["demo"].pop("copies", None)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    custom = project / ".claude" / "skills" / "a" / "SKILL.md"
    custom.write_text("unproven ownership\n", encoding="utf-8")
    result = run("apply", "demo", "--copy")
    assert result.returncode == 0, result.stderr
    assert custom.read_text() == "unproven ownership\n"
    assert run("apply", "demo", "--copy", "--force").returncode == 0
    assert custom.read_text() == "original preset\n"


@pytest.fixture
def remote_backup(tmp_path, sandbox):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    run = sandbox[3]
    result = run("backup", "push", "--repo", remote.as_uri())
    assert result.returncode == 0, result.stdout + result.stderr
    return remote.as_uri()


@pytest.mark.parametrize("fresh", [False, True])
def test_destructive_pull_requires_yes_without_terminal(sandbox, remote_backup, fresh):
    home, project, source, run = sandbox
    if fresh:
        shutil.rmtree(home / ".skill-ctl" / ".git")
        args = ["backup", "pull", "--repo", remote_backup]
    else:
        args = ["backup", "pull", "--force"]
    content = source / "SKILL.md"
    content.write_text("unbacked local work\n", encoding="utf-8")
    result = run(*args)
    assert result.returncode != 0
    assert "--yes" in result.stdout + result.stderr
    assert content.read_text() == "unbacked local work\n"
    accepted = run(*args, "--yes")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert content.read_text() == "original preset\n"


def test_pull_preview_switching_remote_preserves_local_history(tmp_path, sandbox, remote_backup):
    home, project, source, run = sandbox
    remote_copy = tmp_path / "different.git"
    subprocess.run(["git", "clone", "--bare", "-q", remote_backup, str(remote_copy)], check=True)
    git_dir = home / ".skill-ctl" / ".git"
    (git_dir / "local-history-marker").write_text("local history", encoding="utf-8")
    before = {path.relative_to(git_dir): path.read_bytes() for path in git_dir.rglob("*") if path.is_file()}
    content = source / "SKILL.md"
    content.write_text("uncommitted work\n", encoding="utf-8")
    result = run("backup", "pull", "--repo", remote_copy.as_uri(), "--dry-run", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    after = {path.relative_to(git_dir): path.read_bytes() for path in git_dir.rglob("*") if path.is_file()}
    assert after == before
    assert content.read_text() == "uncommitted work\n"


def test_pull_preview_does_not_initialize_local_store(sandbox, remote_backup):
    home, project, source, run = sandbox
    base = home / ".skill-ctl"
    shutil.rmtree(base / ".git")
    for name in ("config.yaml", ".gitignore", ".backup.lock"):
        (base / name).unlink(missing_ok=True)
    result = run("backup", "pull", "--repo", remote_backup, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "presets/demo/a/SKILL.md" in result.stdout
    assert not (base / ".git").exists()
    assert not (base / "config.yaml").exists()
    assert not (base / ".gitignore").exists()
    assert not (base / ".backup.lock").exists()


def test_windows_cleanup_preserves_unrelated_executables(tmp_path, monkeypatch):
    import skill_ctl.self_update as updater

    prefix = tmp_path / "prefix"
    launcher = tmp_path / "launcher"
    directories = [prefix / "Scripts", prefix / "bin", launcher]
    for directory in directories:
        directory.mkdir(parents=True)
        for name in ("skctl.old.exe", "skill-ctl.old.exe", "gold.exe", "unrelated.old.exe", "skctl.exe"):
            (directory / name).write_bytes(b"executable")
    monkeypatch.setattr(updater, "is_windows", lambda: True)
    monkeypatch.setattr(updater.sys, "prefix", str(prefix))
    monkeypatch.setattr(updater.sys, "argv", [str(launcher / "skctl.exe")])
    updater.cleanup_old_executables()
    for directory in directories:
        assert (directory / "gold.exe").read_bytes() == b"executable"
        assert (directory / "unrelated.old.exe").read_bytes() == b"executable"
        assert (directory / "skctl.exe").exists()
        assert not (directory / "skctl.old.exe").exists()
        assert not (directory / "skill-ctl.old.exe").exists()
