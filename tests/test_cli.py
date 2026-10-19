"""Tests for argument preprocessing, config reads and writes, and the apply record."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.mark.parametrize(
    "argv, expected",
    [
        # A command pasted from skills.sh, however much of it came along.
        (["npx", "skills", "add", "owner/repo"], ["add", "owner/repo"]),
        (["skills", "add", "owner/repo"], ["add", "owner/repo"]),
        (["add", "npx", "skills", "add", "owner/repo"], ["add", "owner/repo"]),
        (["add", "skills", "add", "owner/repo"], ["add", "owner/repo"]),
        (["add", "add", "owner/repo"], ["add", "owner/repo"]),
        # ... or pasted as one quoted argument, flags and all.
        (["add", "npx skills add owner/repo --skill foo"], ["add", "owner/repo", "--skill", "foo"]),
        (["add", "skills add owner/repo"], ["add", "owner/repo"]),
        # Flags already given alongside a quoted paste survive it.
        (["add", "npx skills add owner/repo", "-y"], ["add", "owner/repo", "-y"]),
        # Nothing to clean up: left exactly as typed.
        (["add", "owner/repo", "--skill", "add"], ["add", "owner/repo", "--skill", "add"]),
        (["apply", "my-preset"], ["apply", "my-preset"]),
        ([], []),
        (["add"], ["add"]),
        # Aliases, including the one that implies its own action.
        (["restore"], ["backup", "pull"]),
        (["sync", "push"], ["backup", "push"]),
        (["sync"], ["sync"]),
        (["sync", "status"], ["sync", "status"]),
        (["sync", "--dry-run"], ["sync", "--dry-run"]),
        # A bare -s means "ask me", but only where apply can ask.
        (["apply", "-s"], ["apply", "-s", "?"]),
        (["add", "owner/repo", "-s"], ["add", "owner/repo", "-s"]),
    ],
)
def test_preprocess_args(argv, expected):
    from skill_ctl.cli import preprocess_args

    assert preprocess_args(argv) == expected


def test_windows_pasted_command_keeps_path_separators(monkeypatch):
    from skill_ctl import cli

    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    command = r'npx skills add owner/repo --project "C:\Users\Me\My Project"'
    assert cli.split_pasted_command(command)[-1] == r"C:\Users\Me\My Project"


def run_skctl(home, *args):
    return subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", *args],
        env=dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO_ROOT)),
        capture_output=True,
        text=True,
    )


def test_bare_skctl_shows_location_and_counts(tmp_path):
    """Nothing else tells you at a glance where ~/.skill-ctl is or what's in it."""
    presets = tmp_path / ".skill-ctl" / "presets"
    for preset, names in {"frontend": ["tdd"], "writing": ["humanizer", "how"]}.items():
        for name in names:
            d = presets / preset / ".agents" / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

    res = run_skctl(tmp_path)
    assert res.returncode == 0
    out = res.stdout + res.stderr
    assert str(tmp_path / ".skill-ctl") in out
    assert "2 presets" in out
    assert "3 skills" in out


def test_bare_skctl_on_a_fresh_install_shows_zero(tmp_path):
    res = run_skctl(tmp_path)
    assert "0 presets, 0 skills" in res.stdout + res.stderr


def test_help_does_not_show_the_status_line(tmp_path):
    res = run_skctl(tmp_path, "--help")
    assert res.returncode == 0, res.stderr
    assert "0 presets, 0 skills" not in (res.stdout + res.stderr)


@pytest.mark.parametrize("flag", ["version", "--version", "-V"])
def test_version_is_answered_locally(tmp_path, flag):
    """It must work in a broken install: no network, no config, no theme."""
    from skill_ctl import __version__

    res = run_skctl(tmp_path, flag)
    assert res.returncode == 0, res.stderr
    assert res.stdout == f"skctl {__version__}\n"
    assert not (tmp_path / ".skill-ctl").exists()


def test_preset_action_help_hides_unrelated_positionals(tmp_path):
    res = run_skctl(tmp_path, "presets", "create", "--help")

    assert res.returncode == 0, res.stderr
    assert "skctl presets create <name>" in res.stdout
    assert "new-name" not in res.stdout
    assert "source-names" not in res.stdout


def test_a_subcommand_error_does_not_show_the_status_line(tmp_path):
    """The extra line is only for the truly bare command, not every sys.exit."""
    res = run_skctl(tmp_path, "apply", "does-not-exist")
    assert res.returncode != 0
    assert "0 presets, 0 skills" not in (res.stdout + res.stderr)


def test_add_accepts_an_omitted_package_with_preset(tmp_path):
    res = run_skctl(tmp_path, "add", "--preset", "writing")
    assert res.returncode == 1
    assert "requires a terminal" in res.stdout + res.stderr
    assert "required argument" not in (res.stdout + res.stderr).lower()


def test_remove_accepts_an_omitted_skill_with_preset(tmp_path):
    skill = tmp_path / ".skill-ctl" / "presets" / "writing" / ".agents" / "skills" / "tdd"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# tdd\n", encoding="utf-8")

    res = run_skctl(tmp_path, "remove", "--preset", "writing")
    assert res.returncode == 1
    assert "requires a terminal" in res.stdout + res.stderr
    assert "required argument" not in (res.stdout + res.stderr).lower()


def test_load_config_creates_nothing(tmp_path):
    """Resolving a setting must not touch the disk; only ensure_config writes."""
    home = tmp_path / "home"
    home.mkdir()
    res = subprocess.run(
        [sys.executable, "-c", "from skill_ctl.config import load_config; load_config()"],
        env=dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO_ROOT)),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert not (home / ".skill-ctl").exists(), "a read created ~/.skill-ctl"


def test_config_edit_splits_editor_command(monkeypatch, tmp_path):
    """$EDITOR commonly includes a wait flag, e.g. `code --wait`."""
    from skill_ctl import config_commands

    path = tmp_path / "config.yaml"
    calls = []
    monkeypatch.setenv("EDITOR", "code --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(config_commands, "ensure_config", lambda: path)
    monkeypatch.setattr(config_commands.subprocess, "run", calls.append)

    config_commands.config_cmd("edit")

    assert calls == [["code", "--wait", str(path)]]


def test_config_edit_prefers_visual_over_editor(monkeypatch, tmp_path):
    from skill_ctl import config_commands

    path = tmp_path / "config.yaml"
    calls = []
    monkeypatch.setenv("VISUAL", "subl -w")
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setattr(config_commands, "ensure_config", lambda: path)
    monkeypatch.setattr(config_commands.subprocess, "run", calls.append)

    config_commands.config_cmd("edit")

    assert calls == [["subl", "-w", str(path)]]


def test_config_edit_windows_fallback_to_notepad(monkeypatch, tmp_path):
    from skill_ctl import config_commands

    path = tmp_path / "config.yaml"
    calls = []
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(config_commands.os, "name", "nt")
    monkeypatch.setattr(config_commands.sys, "platform", "win32")
    monkeypatch.setattr(
        config_commands.shutil,
        "which",
        lambda cmd: f"C:\\Windows\\System32\\{cmd}.exe" if cmd == "notepad" else None,
    )
    monkeypatch.setattr(config_commands, "ensure_config", lambda: path)
    monkeypatch.setattr(config_commands.subprocess, "run", calls.append)

    config_commands.config_cmd("edit")

    assert calls == [["C:\\Windows\\System32\\notepad.exe", str(path)]]


def test_config_edit_windows_preserves_backslashes(monkeypatch, tmp_path):
    from skill_ctl import config_commands

    path = tmp_path / "config.yaml"
    calls = []
    monkeypatch.setenv("EDITOR", r'"C:\Program Files\My Editor\edit.exe" --wait')
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(config_commands.os, "name", "nt")
    monkeypatch.setattr(config_commands.sys, "platform", "win32")
    monkeypatch.setattr(config_commands, "ensure_config", lambda: path)
    monkeypatch.setattr(config_commands.subprocess, "run", calls.append)

    config_commands.config_cmd("edit")

    assert calls == [[r"C:\Program Files\My Editor\edit.exe", "--wait", str(path)]]


def test_config_edit_file_not_found_handled(monkeypatch, tmp_path, capsys):
    import pytest

    from skill_ctl import config_commands

    path = tmp_path / "config.yaml"
    monkeypatch.setenv("EDITOR", "nonexistent_editor_binary")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(config_commands, "ensure_config", lambda: path)

    def mock_run(cmd):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(config_commands.subprocess, "run", mock_run)

    with pytest.raises(SystemExit) as exc_info:
        config_commands.config_cmd("edit")
    assert exc_info.value.code == 1


def test_ensure_config_writes_the_file_and_migrates_once(tmp_path):
    home = tmp_path / "home"
    (home / ".skill-ctl").mkdir(parents=True)
    cfg = home / ".skill-ctl" / "config.yaml"

    def run(code):
        res = subprocess.run(
            [sys.executable, "-c", code],
            env=dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO_ROOT)),
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        return res

    cfg.write_text("default_preset: mine\n", encoding="utf-8")
    run("from skill_ctl.config import ensure_config; ensure_config()")
    text = cfg.read_text(encoding="utf-8")
    assert "default_preset: mine" in text
    assert "patch_scope_prompt: true" in text

    run("from skill_ctl.config import ensure_config; ensure_config()")
    assert cfg.read_text(encoding="utf-8").count("patch_scope_prompt") == 1

    # And the value is what load_config then reports.
    res = run(
        "from skill_ctl.config import load_config;"
        " print(load_config()['npx']['patch_scope_prompt'])"
    )
    assert res.stdout.strip().endswith("True")


def test_scope_patch_can_be_switched_off(tmp_path, monkeypatch):
    """The loader edits upstream source, so it has to be possible to opt out."""
    from skill_ctl import runner

    calls = {}

    class Result:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None):
        calls["env"] = env
        return Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/npx")

    monkeypatch.setattr(runner, "load_config", lambda: {"npx": {"patch_scope_prompt": True}})
    runner.run_npx_skills(["add", "owner/repo"])
    assert "NODE_OPTIONS" in calls["env"]
    assert "--import=file:" in calls["env"]["NODE_OPTIONS"]

    monkeypatch.setattr(runner, "load_config", lambda: {"npx": {"patch_scope_prompt": False}})
    runner.run_npx_skills(["add", "owner/repo"])
    assert "NODE_OPTIONS" not in calls["env"]


def test_loader_gives_up_on_a_changed_upstream():
    """No marker to patch means hand back the source untouched, not a half-patch."""
    source = (REPO_ROOT / "skill_ctl" / "assets" / "loader.mjs").read_text(encoding="utf-8")

    assert source.count("return result;") >= 3, "each bail-out returns the original"
    assert "patch_scope_prompt: false" in source, "the warning must name the way out"


@pytest.fixture
def index(tmp_path, monkeypatch):
    """Keep the machine-wide index inside tmp_path, never in the real HOME."""
    from skill_ctl import registry

    monkeypatch.setattr(registry, "APPLIED_FILE", tmp_path / "applied.json")
    return tmp_path / "applied.json"


def test_record_is_the_union_of_every_apply(tmp_path, index):
    """Two applies leave two sets of links, so one entry must name both."""
    from skill_ctl.registry import presets_for, record_apply

    proj = tmp_path / "proj"
    proj.mkdir()

    record_apply(proj, "p", ["tdd"], [".agents/skills"], "symlink")
    record_apply(proj, "p", ["review"], [".claude/skills"], "symlink")

    entry = presets_for(proj)["p"]
    assert entry["skills"] == ["review", "tdd"]
    assert entry["targets"] == [".agents/skills", ".claude/skills"]
    assert entry["mode"] == "symlink"

    # The file on disk says the same thing.
    written = json.loads((proj / "skills-applied.json").read_text(encoding="utf-8"))
    assert written["presets"]["p"]["skills"] == ["review", "tdd"]


def test_a_legacy_record_is_read_and_then_renamed(tmp_path, index):
    """Projects committed a `.skctl`; it keeps working and migrates on the next write."""
    from skill_ctl.registry import presets_for, record_apply

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".skctl").write_text(
        json.dumps(
            {
                "version": 1,
                "presets": {
                    "p": {"skills": ["tdd"], "targets": [".agents/skills"], "mode": "symlink"}
                },
            }
        ),
        encoding="utf-8",
    )

    assert presets_for(proj)["p"]["skills"] == ["tdd"], "the old name must still be read"
    assert not (proj / "skills-applied.json").exists(), "a read must not rewrite anything"

    record_apply(proj, "p", ["review"], [".agents/skills"], "symlink")
    written = json.loads((proj / "skills-applied.json").read_text(encoding="utf-8"))
    assert written["presets"]["p"]["skills"] == ["review", "tdd"], "nothing lost in the move"
    assert not (proj / ".skctl").exists(), "the old file must not linger as a second answer"


def test_record_keeps_presets_apart(tmp_path, index):
    from skill_ctl.registry import forget_apply, presets_for, record_apply

    proj = tmp_path / "proj"
    proj.mkdir()
    record_apply(proj, "one", ["tdd"], [".agents/skills"], "symlink")
    record_apply(proj, "two", ["review"], [".agents/skills"], "copy")

    assert sorted(presets_for(proj)) == ["one", "two"]
    forget_apply(proj, "one")
    assert list(presets_for(proj)) == ["two"]
    forget_apply(proj, "two")
    assert not (proj / "skills-applied.json").exists(), "the file goes when nothing is applied"


def test_bare_skctl_shows_project_context(tmp_path, monkeypatch):
    """Bare skctl must show project path, detected agents, and applied presets."""
    proj = tmp_path / "my-project"
    proj.mkdir()
    (proj / ".claude").mkdir()
    (proj / ".agents").mkdir()
    (proj / "skills-applied.json").write_text(
        json.dumps(
            {
                "version": 1,
                "presets": {
                    "writing": {
                        "skills": ["humanizer", "how", "tdd"],
                        "targets": [".agents/skills"],
                        "mode": "symlink",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli"],
        cwd=proj,
        env=dict(
            os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(REPO_ROOT)
        ),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    out = res.stdout
    assert "Project:" in out
    assert "my-project" in out
    assert "Detected agents: Claude Code (.claude), Antigravity (.agents)" in out
    assert "Applied presets: writing (3 skills)" in out


def test_find_command_is_completely_removed(tmp_path):
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "find"],
        cwd=tmp_path,
        env=dict(
            os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(REPO_ROOT)
        ),
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "No such command 'find'" in res.stderr or "No such command" in (res.stdout + res.stderr)


def test_search_help_shows_unified_options(tmp_path):
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "search", "--help"],
        cwd=tmp_path,
        env=dict(
            os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(REPO_ROOT)
        ),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    out = res.stdout
    assert "--local" in out or "-l" in out
    assert "--remote" in out or "-r" in out


def test_list_command_executes_cleanly(tmp_path):
    proj = tmp_path / "project"
    proj.mkdir()
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "list"],
        cwd=proj,
        env=dict(
            os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(REPO_ROOT)
        ),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "Project Skills" in res.stdout

    res_global = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "list", "-g"],
        cwd=proj,
        env=dict(
            os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(REPO_ROOT)
        ),
        capture_output=True,
        text=True,
    )
    assert res_global.returncode == 0
    assert "Global Skills" in res_global.stdout
