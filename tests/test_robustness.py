"""Hostile or corrupt input must produce a message, never a traceback."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cli(tmp_path):
    """Run skctl in a throwaway HOME and project; returns (home, project, run)."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=str(REPO_ROOT),
        COLUMNS="200",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
    )

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "skill_ctl.cli", *args],
            cwd=project,
            env=env,
            input="",
            capture_output=True,
            text=True,
        )

    return home, project, run


def make_skill(preset_dir: Path, name: str, text: str) -> Path:
    skill = preset_dir / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    return skill


def no_traceback(result):
    assert "Traceback" not in result.stderr + result.stdout, result.stderr[-1500:]


# --- config values of the wrong type fall back to defaults ---------------------------------


@pytest.mark.parametrize(
    "yaml_text",
    [
        "npx:\nbackup:\nprompts:\ncustom_agents:\nglobal_skill_dirs:\napply_targets:\n"
        "default_agents:\ntheme:\n",
        "npx: 3\nbackup: yes\nprompts: [1, 2]\ncustom_agents: x\nglobal_skill_dirs: 5\n"
        'apply_targets: ".agents/skills"\ndefault_agents: claude\ntheme: 7\napply_mode: 9\n'
        "default_destination: 4\nsearch_remotes: 1\nsearch_min_relevance: high\n",
        'apply_targets: [1, null, "", [x]]\nglobal_skill_dirs: [1, null, ""]\n'
        'custom_agents: {x: 5, y: null}\ndefault_agents: [1, null, ""]\n',
        "prompts: {preset_picker_threshold: abc, picker: 5}\n",
    ],
)
def test_load_config_survives_wrong_types(tmp_path, monkeypatch, yaml_text):
    from skill_ctl import config

    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    config.invalidate_config_cache()
    try:
        with pytest.warns(RuntimeWarning) if "npx: 3" in yaml_text else _nullcontext():
            cfg = config.load_config()
    finally:
        config.invalidate_config_cache()

    assert isinstance(cfg["npx"], dict) and isinstance(cfg["backup"], dict)
    assert isinstance(cfg["prompts"], dict) and isinstance(cfg["custom_agents"], dict)
    for key in ("apply_targets", "default_agents", "global_skill_dirs"):
        assert isinstance(cfg[key], list) and all(isinstance(v, str) and v for v in cfg[key]), key
    assert cfg["apply_targets"], "bad apply_targets must not leave nowhere to apply"
    for key in ("theme", "apply_mode", "default_destination"):
        assert isinstance(cfg[key], str), key
    assert isinstance(cfg["search_min_relevance"], float)
    assert isinstance(cfg["prompts"]["preset_picker_threshold"], int)
    # Everything downstream of the config must be usable.
    assert config.get_agent_dir_map(cfg)
    assert config.all_target_dirs(cfg)
    config.global_skill_dirs(cfg)


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_config_with_null_sections_does_not_crash_commands(cli):
    home, _project, run = cli
    make_skill(home / ".skill-ctl" / "presets" / "demo", "a", "# a\n")
    (home / ".skill-ctl" / "config.yaml").write_text(
        "custom_agents:\napply_targets:\nnpx:\nbackup:\n", encoding="utf-8"
    )
    for args in (["status"], ["doctor"], ["apply", "demo", "--yes"], ["unapply", "demo", "--yes"]):
        result = run(*args)
        no_traceback(result)
        assert result.returncode == 0, (args, result.stderr[-500:])


# --- skill text from a remote repo must not break rich markup -------------------------------


def test_info_survives_markup_in_description(cli):
    home, _project, run = cli
    make_skill(
        home / ".skill-ctl" / "presets" / "demo",
        "markup",
        '---\nname: markup\ndescription: "[bold red]x[/] [/nope] [link=http://x]y[/link]"\n---\n',
    )
    result = run("info", "markup")
    no_traceback(result)
    assert result.returncode == 0
    assert "[/nope]" in result.stdout, "the text must be shown literally, not interpreted"


def test_console_print_never_dies_on_bad_markup():
    from skill_ctl.ui import console

    console.print("before [/nope] after")  # raised rich.errors.MarkupError


# --- corrupt lock files -----------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "{", "[]", "null", '"s"', '{"skills": []}', "\xff"])
def test_prune_lockfile_tolerates_corrupt_lock(tmp_path, content):
    from skill_ctl.linker import prune_lockfile

    (tmp_path / "skills-lock.json").write_text(content, encoding="utf-8", errors="replace")
    assert prune_lockfile(tmp_path, {"x"}) == 0
    assert (tmp_path / "skills-lock.json").exists(), "a corrupt lock is left for the user to fix"


@pytest.mark.parametrize("content", ["", "{", "[]", "null"])
def test_unapply_and_edit_survive_corrupt_lock(cli, content):
    home, project, run = cli
    preset = home / ".skill-ctl" / "presets" / "demo"
    make_skill(preset, "a", "# a\n")
    make_skill(preset, "b", "# b\n")
    (preset / "skills-lock.json").write_text(content, encoding="utf-8")
    assert run("apply", "demo", "--yes").returncode == 0
    (project / "skills-lock.json").write_text(content, encoding="utf-8")
    for args in (["unapply", "demo", "--yes"], ["presets", "edit", "demo", "--remove", "a"]):
        result = run(*args)
        no_traceback(result)
        assert result.returncode == 0, (args, result.stderr[-500:])


# --- presets holding symlinks ---------------------------------------------------------------


def test_clone_and_combine_preserve_broken_and_looping_links(cli):
    home, _project, run = cli
    preset = home / ".skill-ctl" / "presets" / "demo"
    skill = make_skill(preset, "a", "# a\n")
    try:
        (skill / "broken").symlink_to("/nonexistent/target")
        (skill / "loop").mkdir()
        (skill / "loop" / "self").symlink_to(skill / "loop", target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")

    for args in (["presets", "clone", "demo", "c1"], ["presets", "combine", "cc", "demo", "c1"]):
        result = run(*args)
        no_traceback(result)
        assert result.returncode == 0, (args, result.stderr[-500:])
    copied = home / ".skill-ctl" / "presets" / "c1" / "a"
    assert (copied / "broken").is_symlink(), "clone must copy the preset exactly, links included"


def test_adding_local_skill_to_preset_drops_links_leaving_the_folder(cli, tmp_path):
    """The folder is copied into a preset that auto-push uploads, so a link out of it is dropped."""
    home, _project, run = cli
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-TOKEN\n", encoding="utf-8")
    source = tmp_path / "skills"
    skill = make_skill(source, "a", "# a\n")
    try:
        (skill / "key.txt").symlink_to(secret)
        (skill / "dangling").symlink_to(tmp_path / "gone")
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")

    result = run("presets", "create", "demo", "--from", str(source))
    no_traceback(result)
    assert result.returncode == 0, result.stderr[-500:]
    copied = home / ".skill-ctl" / "presets" / "demo" / "a"
    assert (copied / "SKILL.md").is_file()
    assert not (copied / "key.txt").exists() and not (copied / "key.txt").is_symlink()
    assert not (copied / "dangling").is_symlink()
    leaked = [
        p
        for p in (home / ".skill-ctl").rglob("*")
        if p.is_file() and "TOP-SECRET" in p.read_text(errors="replace")
    ]
    assert leaked == []


# --- preset names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["presets", "create"],
        ["presets", "delete"],
        ["presets", "history"],
        ["diff"],
        ["apply"],
    ],
)
def test_overlong_preset_name_is_rejected_cleanly(cli, args):
    _home, _project, run = cli
    result = run(*args, "a" * 300)
    no_traceback(result)
    assert result.returncode != 0


def test_valid_preset_name():
    from skill_ctl.config import valid_preset_name

    assert valid_preset_name("demo") and valid_preset_name("ünï") and valid_preset_name("a b")
    assert valid_preset_name("é" * 127)  # 254 bytes
    for bad in ("", ".", "..", "a/b", "a\\b", "a" * 256, "é" * 128, "a\0b", None, 5):
        assert not valid_preset_name(bad), bad
