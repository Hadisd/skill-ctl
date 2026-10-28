"""Tests for `skctl backup` against a local bare repository standing in for GitHub."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

GIT_ENV = {
    "GIT_AUTHOR_NAME": "skctl test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "skctl test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def make_home(tmp_path: Path, name: str, with_preset: bool = True) -> dict:
    home = tmp_path / name
    if with_preset:
        skill = home / ".skill-ctl" / "presets" / "demo" / ".agents" / "skills" / "tdd"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# tdd\n", encoding="utf-8")
    home.mkdir(parents=True, exist_ok=True)
    return dict(
        os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO_ROOT), **GIT_ENV
    )


@pytest.fixture
def remote(tmp_path):
    """A bare repo acting as the user's GitHub backup."""
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(path)], check=True)
    return f"file://{path}"


def skctl(env, *args, **kwargs):
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", *args],
        cwd=str(Path(env.get("USERPROFILE") or env["HOME"])),
        env=env,
        capture_output=True,
        text=True,
        **kwargs,
    )


def test_github_repo_shorthand_becomes_a_clone_url():
    from skill_ctl.presets import github_repo_url

    assert github_repo_url("owner/presets") == "https://github.com/owner/presets.git"


def test_push_then_restore_on_another_machine(tmp_path, remote):
    first = make_home(tmp_path, "home1")
    res = skctl(first, "backup", "push", "--repo", remote, "-m", "first backup")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (Path(first["HOME"]) / ".skill-ctl" / ".git").is_dir()

    # A second machine restores the same presets from the backup.
    second = make_home(tmp_path, "home2", with_preset=False)
    res = skctl(second, "backup", "pull", "--repo", remote, "-y")
    assert res.returncode == 0, res.stdout + res.stderr
    restored = (
        Path(second["HOME"]) / ".skill-ctl" / "presets" / "demo" / ".agents" / "skills" / "tdd"
    )
    assert (restored / "SKILL.md").read_text(encoding="utf-8") == "# tdd\n"


def test_push_is_idempotent(tmp_path, remote):
    env = make_home(tmp_path, "home1")
    assert skctl(env, "backup", "push", "--repo", remote).returncode == 0
    res = skctl(env, "backup", "push")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Nothing changed" in res.stdout


def test_status_reports_uncommitted_and_missing_repo(tmp_path, remote):
    env = make_home(tmp_path, "home1")
    res = skctl(env, "backup", "status")
    assert "not backed up yet" in res.stdout

    assert skctl(env, "backup", "push", "--repo", remote).returncode == 0
    new_skill = (
        Path(env["HOME"]) / ".skill-ctl" / "presets" / "demo" / ".agents" / "skills" / "review"
    )
    new_skill.mkdir(parents=True)
    (new_skill / "SKILL.md").write_text("# review\n", encoding="utf-8")

    res = skctl(env, "backup", "status")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "uncommitted change" in res.stdout


def test_restore_alias_pulls(tmp_path, remote):
    env = make_home(tmp_path, "home1")
    assert skctl(env, "backup", "push", "--repo", remote).returncode == 0

    other = make_home(tmp_path, "home2", with_preset=False)
    res = skctl(other, "restore", "--repo", remote, "-y")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (Path(other["HOME"]) / ".skill-ctl" / "presets" / "demo").is_dir()


def test_init_without_repo_or_gh_fails_clearly(tmp_path):
    import shutil

    env = make_home(tmp_path, "home1")
    if sys.platform == "win32":
        env["PATH"] = os.pathsep.join(
            p
            for p in os.environ.get("PATH", "").split(os.pathsep)
            if "github cli" not in p.lower() and not (Path(p) / "gh.exe").is_file()
        )
    else:
        bin_dir = tmp_path / "empty-bin"
        bin_dir.mkdir(exist_ok=True)
        git_bin = shutil.which("git")
        if git_bin:
            (bin_dir / "git").symlink_to(git_bin)
        (bin_dir / "python3").symlink_to(sys.executable)
        (bin_dir / "python").symlink_to(sys.executable)
        env["PATH"] = str(bin_dir)

    res = skctl(env, "backup", "init")
    assert res.returncode == 1
    assert "backup init --repo" in res.stdout + res.stderr


def test_empty_preset_survives_the_round_trip(tmp_path, remote):
    """Git stores no empty directories, so an unfilled preset needs a .gitkeep."""
    first = make_home(tmp_path, "home1")
    (Path(first["HOME"]) / ".skill-ctl" / "presets" / "blank").mkdir(parents=True)
    assert skctl(first, "backup", "push", "--repo", remote).returncode == 0

    second = make_home(tmp_path, "home2", with_preset=False)
    assert skctl(second, "backup", "pull", "--repo", remote, "-y").returncode == 0
    assert (Path(second["HOME"]) / ".skill-ctl" / "presets" / "blank").is_dir()


def test_restore_dry_run_lists_changes_without_restoring(tmp_path, remote):
    first = make_home(tmp_path, "home1")
    assert skctl(first, "backup", "push", "--repo", remote).returncode == 0

    second = make_home(tmp_path, "home2", with_preset=False)
    res = skctl(second, "backup", "pull", "--repo", remote, "--dry-run")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Restore preview" in res.stdout
    assert "presets/demo" in res.stdout
    assert not (Path(second["HOME"]) / ".skill-ctl" / "presets" / "demo").exists()


def test_export_and_import_preset_with_preview_and_rename(tmp_path):
    first = make_home(tmp_path, "home1")
    archive = tmp_path / "demo.skctl-preset.zip"
    res = skctl(first, "presets", "export", "demo", "--output", str(archive))
    assert res.returncode == 0, res.stdout + res.stderr
    assert archive.is_file()

    second = make_home(tmp_path, "home2", with_preset=False)
    preview = skctl(second, "presets", "import", str(archive), "--rename", "copied", "--dry-run")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "Import preview" in preview.stdout
    assert not (Path(second["HOME"]) / ".skill-ctl" / "presets" / "copied").exists()

    imported = skctl(second, "presets", "import", str(archive), "--rename", "copied")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    skill = (
        Path(second["HOME"])
        / ".skill-ctl"
        / "presets"
        / "copied"
        / ".agents"
        / "skills"
        / "tdd"
        / "SKILL.md"
    )
    assert skill.read_text(encoding="utf-8") == "# tdd\n"


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("empty_subdirectory", [False, True])
def test_empty_preset_archive_round_trips(tmp_path, replace, empty_subdirectory):
    env = make_home(tmp_path, "home")
    presets = Path(env["HOME"]) / ".skill-ctl/presets"
    source = presets / "empty"
    source.mkdir()
    if empty_subdirectory:
        (source / "references").mkdir()
    archive = tmp_path / "empty.zip"
    result = skctl(env, "presets", "export", "empty", "-o", str(archive))
    assert result.returncode == 0, result.stdout + result.stderr
    target = presets / "imported"
    if replace:
        target.mkdir()
        (target / "old.txt").write_text("Old preset\n", encoding="utf-8")
    result = skctl(
        env,
        "presets",
        "import",
        str(archive),
        "--rename",
        "imported",
        *(["--replace"] if replace else []),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.is_dir()
    assert not (target / "old.txt").exists()
    if empty_subdirectory:
        assert (target / "references").is_dir()


def test_import_preset_from_a_repository(tmp_path, remote):
    first = make_home(tmp_path, "home1")
    research = (
        Path(first["HOME"])
        / ".skill-ctl"
        / "presets"
        / "research"
        / ".agents"
        / "skills"
        / "papers"
    )
    research.mkdir(parents=True)
    (research / "SKILL.md").write_text("# papers\n", encoding="utf-8")
    assert skctl(first, "backup", "push", "--repo", remote).returncode == 0

    second = make_home(tmp_path, "home2", with_preset=False)
    preview = skctl(
        second,
        "presets",
        "import",
        remote,
        "--preset",
        "demo",
        "--rename",
        "community-demo",
        "--dry-run",
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    imported = Path(second["HOME"]) / ".skill-ctl" / "presets" / "community-demo"
    assert "Import preview" in preview.stdout
    assert not imported.exists()

    result = skctl(second, "presets", "import", remote, "--preset", "demo", "--preset", "research")
    assert result.returncode == 0, result.stdout + result.stderr
    demo = Path(second["HOME"]) / ".skill-ctl" / "presets" / "demo"
    assert (demo / ".agents" / "skills" / "tdd" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# tdd\n"
    assert (
        Path(second["HOME"])
        / ".skill-ctl"
        / "presets"
        / "research"
        / ".agents"
        / "skills"
        / "papers"
        / "SKILL.md"
    ).read_text(encoding="utf-8") == "# papers\n"


def test_backup_excludes_and_untracks_cache(tmp_path, remote):
    first = make_home(tmp_path, "home1")
    cache_dir = Path(first["HOME"]) / ".skill-ctl" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "catalog.json").write_text('{"skills": []}', encoding="utf-8")

    # Push initial backup
    res = skctl(first, "backup", "push", "--repo", remote, "-m", "first backup")
    assert res.returncode == 0, res.stdout + res.stderr

    # Second machine pulling should not receive cache/
    second = make_home(tmp_path, "home2", with_preset=False)
    res = skctl(second, "backup", "pull", "--repo", remote, "-y")
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (Path(second["HOME"]) / ".skill-ctl" / "cache").exists()

    # If cache was manually added in an older repo, next push should untrack it
    base_dir = Path(first["HOME"]) / ".skill-ctl"
    (cache_dir / "old_cached.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "-C", str(base_dir), "add", "-f", "cache/old_cached.json"], check=True)
    subprocess.run(
        ["git", "-C", str(base_dir), "commit", "-m", "add old cache manually"],
        check=True,
        env=first,
    )

    # Next push should untrack cache and metadata cache
    (base_dir / ".metadata_cache.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "-C", str(base_dir), "add", "-f", ".metadata_cache.json"], check=True)
    subprocess.run(
        ["git", "-C", str(base_dir), "commit", "-m", "add metadata cache manually"],
        check=True,
        env=first,
    )

    res = skctl(first, "backup", "push")
    assert res.returncode == 0, res.stdout + res.stderr

    tracked_cache = subprocess.run(
        ["git", "-C", str(base_dir), "ls-files", "cache", ".metadata_cache.json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tracked_cache == ""


def test_backup_push_and_pull_global_skills(tmp_path, remote):
    first = make_home(tmp_path, "home1")
    home1 = Path(first["HOME"])
    global_skill = home1 / ".agents" / "skills" / "caveman"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text("# caveman\n", encoding="utf-8")
    lock_file = home1 / ".agents" / ".skill-lock.json"
    lock_file.write_text(
        '{"version": 1, "skills": {"caveman": {"source": "owner/caveman"}}}\n',
        encoding="utf-8",
    )
    # Symlink into presets should NOT be duplicated as a standalone global skill
    preset_link = home1 / ".agents" / "skills" / "tdd"
    preset_link.symlink_to(home1 / ".skill-ctl" / "presets" / "demo" / ".agents" / "skills" / "tdd")

    res = skctl(first, "backup", "push", "--repo", remote, "-g")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (
        home1 / ".skill-ctl" / "global" / ".agents" / "skills" / "caveman" / "SKILL.md"
    ).is_file()
    assert not (home1 / ".skill-ctl" / "global" / ".agents" / "skills" / "tdd").exists()

    # Restore on a second machine with -g
    second = make_home(tmp_path, "home2", with_preset=False)
    home2 = Path(second["HOME"])
    res = skctl(second, "backup", "pull", "--repo", remote, "-y", "--global")
    assert res.returncode == 0, res.stdout + res.stderr
    restored_global = home2 / ".agents" / "skills" / "caveman" / "SKILL.md"
    assert restored_global.is_file()
    assert restored_global.read_text(encoding="utf-8") == "# caveman\n"
    restored_lock = home2 / ".agents" / ".skill-lock.json"
    assert restored_lock.is_file()
    assert "owner/caveman" in restored_lock.read_text(encoding="utf-8")


def test_backup_status_reports_uncommitted_global_skills(tmp_path, remote):
    first = make_home(tmp_path, "home1")
    home1 = Path(first["HOME"])
    assert skctl(first, "backup", "push", "--repo", remote, "-g").returncode == 0

    new_global = home1 / ".agents" / "skills" / "firecrawl"
    new_global.mkdir(parents=True)
    (new_global / "SKILL.md").write_text("# firecrawl\n", encoding="utf-8")

    # Without -g, status only checks ~/.skill-ctl
    res_plain = skctl(first, "backup", "status")
    assert res_plain.returncode == 0
    assert "Backup is current" in res_plain.stdout

    # With -g, status detects the new global skill without writing to ~/.skill-ctl
    res_global = skctl(first, "backup", "status", "-g")
    assert res_global.returncode == 0
    assert "uncommitted change" in res_global.stdout
    assert "global/.agents/skills/firecrawl" in res_global.stdout
    assert not (home1 / ".skill-ctl" / "global").exists()


def test_backup_global_preserves_all_agent_directories(tmp_path, remote):
    first = make_home(tmp_path, "home1")
    home1 = Path(first["HOME"])

    agents_skill = home1 / ".agents" / "skills" / "caveman"
    agents_skill.mkdir(parents=True)
    (agents_skill / "SKILL.md").write_text("# caveman\n", encoding="utf-8")

    cursor_dir = home1 / ".cursor" / "skills"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "caveman").symlink_to("../../.agents/skills/caveman", target_is_directory=True)

    hermes_skill = home1 / ".hermes" / "skills" / "hermes-only"
    hermes_skill.mkdir(parents=True)
    (hermes_skill / "SKILL.md").write_text("# hermes-only\n", encoding="utf-8")

    res = skctl(first, "backup", "push", "--repo", remote, "-g")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (home1 / ".skill-ctl/global/.agents/skills/caveman/SKILL.md").is_file()
    assert (home1 / ".skill-ctl/global/.cursor/skills/caveman/SKILL.md").is_file()
    assert (home1 / ".skill-ctl/global/.hermes/skills/hermes-only/SKILL.md").is_file()

    second = make_home(tmp_path, "home2", with_preset=False)
    home2 = Path(second["HOME"])
    res = skctl(second, "backup", "pull", "--repo", remote, "-y", "-g")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (home2 / ".agents/skills/caveman/SKILL.md").read_text(encoding="utf-8") == "# caveman\n"
    assert (home2 / ".cursor/skills/caveman/SKILL.md").read_text(encoding="utf-8") == "# caveman\n"
    assert (home2 / ".hermes/skills/hermes-only/SKILL.md").read_text(
        encoding="utf-8"
    ) == "# hermes-only\n"
