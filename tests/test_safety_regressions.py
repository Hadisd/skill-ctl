"""Regression tests for destructive CLI operations, with isolated homes."""

import json
import os
from pathlib import Path
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
