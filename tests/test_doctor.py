"""Tests for the applied-project record, bare unapply, resync, and doctor."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_skill(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return d


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    presets = home / ".skill-ctl" / "presets"
    for preset_name, skills in (("mypreset", ["tdd", "review"]), ("otherp", ["tdd"])):
        for skill in skills:
            make_skill(presets / preset_name / ".agents" / "skills", skill)
    return {
        "home": home,
        "proj": proj,
        "presets": presets,
        "applied": home / ".skill-ctl" / "applied.json",
        "env": dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO_ROOT)),
    }


def skctl(env, *args, cwd=None, **kwargs):
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", *args],
        cwd=str(cwd or env["proj"]),
        env=env["env"],
        capture_output=True,
        text=True,
        **kwargs,
    )


def flat(res) -> str:
    """Console output on one line: rich wraps at the terminal width mid-sentence."""
    return " ".join(res.stdout.split())


def applied(env) -> dict:
    return json.loads(env["applied"].read_text(encoding="utf-8"))["projects"]


def test_apply_records_the_project(env):
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0
    entry = applied(env)[str(env["proj"])]["mypreset"]
    assert entry["skills"] == ["tdd"]
    assert entry["mode"] == "symlink"
    assert ".agents/skills" in entry["targets"]


def test_bare_unapply_uses_the_record_not_the_default_preset(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    res = skctl(env, "unapply")
    assert res.returncode == 0, res.stderr
    assert "mypreset" in flat(res)
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()
    assert not env["applied"].exists()  # last record gone, so is the file


def test_resync_reapplies_recorded_skills_only(env):
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0
    shutil.rmtree(env["proj"] / ".agents")

    res = skctl(env, "apply", "--resync")
    assert res.returncode == 0, res.stderr
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()
    # 'review' was never applied here, so a resync must not add it.
    assert not (env["proj"] / ".agents" / "skills" / "review").exists()


def test_resync_reports_a_skill_dropped_from_the_preset(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    shutil.rmtree(env["presets"] / "mypreset" / ".agents" / "skills" / "review")

    res = skctl(env, "apply", "--resync")
    assert res.returncode == 0, res.stderr
    assert "review" in flat(res)
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()


def test_presets_delete_warns_about_projects_using_it(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    res = skctl(env, "presets", "delete", "mypreset")
    assert res.returncode == 0, res.stderr
    assert str(env["proj"]) in flat(res)
    assert "doctor" in flat(res)


def test_doctor_finds_and_fixes_links_left_by_a_deleted_preset(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    shutil.rmtree(env["presets"] / "mypreset")

    res = skctl(env, "doctor")
    assert res.returncode == 0, res.stderr
    assert "broken" in flat(res)
    link = env["proj"] / ".agents" / "skills" / "tdd"
    assert link.is_symlink() and not link.exists()  # reported, not yet removed

    res = skctl(env, "doctor", "--fix")
    assert res.returncode == 0, res.stderr
    assert not link.is_symlink()
    assert not (env["proj"] / ".agents").exists()  # emptied directories go too
    assert not env["applied"].exists()


def test_doctor_all_checks_every_recorded_project(env, tmp_path):
    other = tmp_path / "proj2"
    other.mkdir()
    assert skctl(env, "apply", "mypreset").returncode == 0
    assert skctl(env, "apply", "otherp", cwd=other).returncode == 0
    shutil.rmtree(env["presets"] / "otherp")

    res = skctl(env, "doctor", "--all")
    assert res.returncode == 0, res.stderr
    assert str(other) in flat(res)
    assert "broken" in flat(res)


def test_doctor_prunes_projects_that_are_gone(env, tmp_path):
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    assert skctl(env, "apply", "mypreset", cwd=doomed).returncode == 0
    shutil.rmtree(doomed)

    res = skctl(env, "doctor", "--all", "--fix")
    assert res.returncode == 0, res.stderr
    assert "no longer exists" in flat(res)
    assert not env["applied"].exists()


def test_doctor_keeps_the_record_of_copies_from_a_deleted_preset(env):
    """Forgetting them would strand real directories nothing else can name."""
    assert skctl(env, "apply", "mypreset", "--copy").returncode == 0
    shutil.rmtree(env["presets"] / "mypreset")

    res = skctl(env, "doctor", "--fix")
    assert res.returncode == 0, res.stderr
    assert "unapply mypreset --force" in flat(res)
    assert (env["proj"] / "skills-applied.json").exists(), "the record of the copies must survive"

    assert skctl(env, "unapply", "mypreset", "--force").returncode == 0
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()
    assert not (env["proj"] / "skills-applied.json").exists()


def test_healthy_project_reports_clean(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    res = skctl(env, "doctor")
    assert "looks fine" in flat(res)


@pytest.mark.parametrize("shell", ["fish", "bash", "zsh", "powershell"])
def test_completion_scripts_mention_presets(env, shell):
    res = skctl(env, "completion", shell)
    assert res.returncode == 0, res.stderr
    assert ".skill-ctl/presets" in res.stdout
    assert "apply" in res.stdout


def test_old_config_gains_new_sections(env):
    """A config.yaml from before `backup` existed gets the section appended."""
    cfg = env["home"] / ".skill-ctl" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("default_preset: mypreset\ncustom_agents: {}\n", encoding="utf-8")

    assert skctl(env, "presets").returncode == 0
    text = cfg.read_text(encoding="utf-8")
    assert "default_preset: mypreset" in text  # their settings survive
    assert "auto_push: false" in text

    # Appending happens once, not on every command.
    assert skctl(env, "presets").returncode == 0
    assert cfg.read_text(encoding="utf-8").count("auto_push") == 1


def test_apply_writes_a_project_file(env):
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0
    record = json.loads((env["proj"] / "skills-applied.json").read_text(encoding="utf-8"))
    assert record["presets"]["mypreset"]["skills"] == ["tdd"]


def test_cloned_project_resyncs_without_the_index(env, tmp_path):
    """A record that travels with the project is enough on a fresh machine."""
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0

    clone = tmp_path / "clone"
    clone.mkdir()
    shutil.copy(env["proj"] / "skills-applied.json", clone / "skills-applied.json")
    env["applied"].unlink()  # this machine has no index at all

    res = skctl(env, "apply", "--resync", cwd=clone)
    assert res.returncode == 0, res.stderr
    assert (clone / ".agents" / "skills" / "tdd").is_symlink()
    # Reading the project file re-indexes it, so cross-project commands see it.
    assert str(clone) in applied(env)


def test_unapply_removes_the_project_file(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    assert skctl(env, "unapply").returncode == 0
    assert not (env["proj"] / "skills-applied.json").exists()


def test_doctor_fix_rewrites_the_project_file(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    assert skctl(env, "apply", "otherp").returncode == 0
    shutil.rmtree(env["presets"] / "otherp")

    assert skctl(env, "doctor", "--fix").returncode == 0
    record = json.loads((env["proj"] / "skills-applied.json").read_text(encoding="utf-8"))
    assert list(record["presets"]) == ["mypreset"]
