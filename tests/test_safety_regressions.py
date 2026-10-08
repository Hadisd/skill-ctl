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


@pytest.mark.parametrize("selection, expected", [
    (["--skill", "a"], {".agents/skills/b", ".claude/skills/b"}),
    (["--agent", "universal"], {".claude/skills/a", ".claude/skills/b"}),
    (["--agent", "universal", "--skill", "a"],
     {".claude/skills/a", ".agents/skills/b", ".claude/skills/b"}),
])
def test_filtered_unapply_preserves_remaining_selection(sandbox, selection, expected):
    home, project, source, run = sandbox
    other = source.parent / "b"
    other.mkdir()
    (other / "SKILL.md").write_text("second skill\n", encoding="utf-8")
    assert run("apply", "demo", "--copy").returncode == 0
    lock_path = project / "skills-lock.json"
    lock_path.write_text(json.dumps({"version": 1, "skills": {"a": {}, "b": {}}}), encoding="utf-8")
    result = run("unapply", "demo", "--force", *selection)
    assert result.returncode == 0, result.stdout + result.stderr
    record_path = project / "skills-applied.json"
    assert record_path.exists()
    entry = json.loads(record_path.read_text())["presets"]["demo"]
    assert set(entry["skills"]) == {Path(path).name for path in expected}
    assert set(json.loads(lock_path.read_text())["skills"]) == set(entry["skills"])
    index = json.loads((home / ".skill-ctl" / "applied.json").read_text())
    assert "demo" in index["projects"][str(project)]
    for directory in (".agents", ".claude"):
        if (project / directory).exists():
            shutil.rmtree(project / directory)
    synced = run("apply", "--resync")
    assert synced.returncode == 0, synced.stdout + synced.stderr
    actual = {str(path.parent.relative_to(project)) for path in project.rglob("SKILL.md")}
    assert actual == expected


def test_filtered_unapply_revokes_removed_copy_ownership(sandbox):
    home, project, source, run = sandbox
    assert run("apply", "demo", "--copy").returncode == 0
    assert run("unapply", "demo", "--agent", "universal", "--force").returncode == 0
    custom = project / ".agents" / "skills" / "a"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text("new unrelated work\n", encoding="utf-8")
    result = run("apply", "demo", "--copy")
    assert result.returncode == 0, result.stderr
    assert (custom / "SKILL.md").read_text() == "new unrelated work\n"


@pytest.mark.parametrize("global_apply", [False, True])
def test_rename_keeps_absolute_relative_and_custom_agent_links_working(sandbox, global_apply):
    home, project, source, run = sandbox
    flags = ["--global"] if global_apply else []
    destination = home if global_apply else project
    assert run("apply", "demo", *flags).returncode == 0
    assert run("apply", "demo", "--agent", "goose", *flags).returncode == 0
    relative = destination / ".agents" / "skills" / "a"
    relative.unlink()
    relative.symlink_to(os.path.relpath(source, relative.parent), target_is_directory=True)
    unrelated = destination / ".cursor" / "skills" / "unrelated"
    unrelated.parent.mkdir(parents=True)
    unrelated.symlink_to(project, target_is_directory=True)
    result = run("presets", "rename", "demo", "renamed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not os.path.isabs(os.readlink(relative))
    for directory in (".agents", ".goose"):
        link = destination / directory / "skills" / "a"
        assert link.is_symlink()
        assert (link / "SKILL.md").read_text() == "original preset\n"
    assert unrelated.resolve() == project
    assert run("unapply", "renamed", *flags).returncode == 0
    assert not relative.is_symlink()
    assert not (destination / ".goose" / "skills" / "a").is_symlink()


def test_rename_keeps_internal_absolute_skill_links_working(sandbox):
    home, project, source, run = sandbox
    alias = source.parent / ".claude" / "skills" / "a"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(source, target_is_directory=True)
    assert run("apply", "demo").returncode == 0
    result = run("presets", "rename", "demo", "renamed")
    assert result.returncode == 0, result.stdout + result.stderr
    moved_alias = home / ".skill-ctl" / "presets" / "renamed" / ".claude" / "skills" / "a"
    assert (moved_alias / "SKILL.md").read_text() == "original preset\n"
    assert (project / ".agents" / "skills" / "a" / "SKILL.md").read_text() == "original preset\n"


def test_rename_rolls_back_when_link_replacement_fails(sandbox, monkeypatch):
    from skill_ctl.linker import rename_preset_directory

    home, project, source, run = sandbox
    targets = [".agents/skills", ".claude/skills"]
    for target in targets:
        directory = project / target
        directory.mkdir(parents=True)
        (directory / "a").symlink_to(source, target_is_directory=True)
    replace = Path.replace

    def fail_second_replacement(path, destination):
        if path.name == "updated" and ".claude" in destination.parts:
            raise PermissionError("cannot replace link")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_second_replacement)
    old = source.parent
    new = old.with_name("renamed")
    with pytest.raises(PermissionError, match="cannot replace link"):
        rename_preset_directory(old, new, {project: targets})
    assert old.is_dir()
    assert not new.exists()
    for target in targets:
        assert (project / target / "a" / "SKILL.md").read_text() == "original preset\n"
    assert not list(project.rglob(".skctl-rename-*"))


def test_get_github_token_from_env_or_gh(monkeypatch):
    from skill_ctl import catalog
    monkeypatch.setattr(catalog, "_GH_TOKEN_CACHE", None)
    monkeypatch.setenv("GITHUB_TOKEN", "token_env_123")
    assert catalog.get_github_token() == "token_env_123"

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    class FakeRes:
        returncode = 0
        stdout = "gh_auth_token_789\n"

    monkeypatch.setattr(catalog.shutil, "which", lambda cmd: "/usr/bin/gh")
    monkeypatch.setattr(catalog.subprocess, "run", lambda *args, **kwargs: FakeRes())

    token = catalog.get_github_token()
    assert token == "gh_auth_token_789"
    assert catalog._GH_TOKEN_CACHE == "gh_auth_token_789"


def test_update_all_concurrency(sandbox, monkeypatch):
    from skill_ctl import skills as skills_mod
    home, project, source, run = sandbox
    preset1 = home / ".skill-ctl" / "presets" / "p1"
    preset2 = home / ".skill-ctl" / "presets" / "p2"
    preset1.mkdir(parents=True)
    preset2.mkdir(parents=True)
    (preset1 / "skills-lock.json").write_text("{}", encoding="utf-8")
    (preset2 / "skills-lock.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(skills_mod, "PRESETS_DIR", home / ".skill-ctl" / "presets")

    calls = []

    def fake_run(args, cwd):
        calls.append((args, cwd))
        return 0

    monkeypatch.setattr(skills_mod, "run_npx_skills", fake_run)
    monkeypatch.setattr(skills_mod, "maybe_auto_push", lambda reason: None)

    skills_mod.update(all_presets=True, yes=True, concurrency=2)

    assert len(calls) == 2
    cwds = {c[1] for c in calls}
    assert cwds == {str(preset1), str(preset2)}
