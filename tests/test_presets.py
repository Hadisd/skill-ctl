"""Tests for the destructive paths in apply/unapply, and for arg preprocessing."""

import json
import os
import shutil
import subprocess
import sys
import time
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
    """A sandboxed HOME with two presets, plus an empty project directory."""
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
        "env": dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO_ROOT)),
    }


def skctl(env, *args, **kwargs):
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", *args],
        cwd=env["proj"],
        env=env["env"],
        capture_output=True,
        text=True,
        **kwargs,
    )


def test_apply_links_skills_into_default_targets(env):
    res = skctl(env, "apply", "mypreset")
    assert res.returncode == 0, res.stderr
    for rel in (".agents/skills", ".claude/skills"):
        for skill in ("tdd", "review"):
            link = env["proj"] / rel / skill
            assert link.is_symlink(), f"{rel}/{skill} was not linked"
            assert (link / "SKILL.md").exists()


def test_presets_applied_lists_all_projects(env):
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0
    res = skctl(env, "presets", "applied")
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "Applied projects" in out
    assert env["proj"].name in out
    assert "mypreset" in out
    assert "symlink" in out


def test_presets_rename_updates_project_record(env):
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0
    res = skctl(env, "presets", "rename", "mypreset", "frontend")
    assert res.returncode == 0, res.stderr
    assert (env["presets"] / "frontend").is_dir()
    assert not (env["presets"] / "mypreset").exists()
    record = json.loads((env["proj"] / "skills-applied.json").read_text())
    assert "frontend" in record["presets"]
    assert "mypreset" not in record["presets"]


def test_status_reports_missing_preset(env):
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0
    (env["presets"] / "mypreset").rename(env["presets"] / "gone")
    res = skctl(env, "status")
    assert res.returncode == 0, res.stderr
    assert "preset missing" in res.stdout + res.stderr


def test_apply_skill_filter_applies_only_named(env):
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()
    assert not (env["proj"] / ".agents" / "skills" / "review").exists()


def test_apply_dry_run_shows_links_without_writing(env):
    res = skctl(env, "apply", "mypreset", "--dry-run")

    assert res.returncode == 0, res.stderr
    assert "Would link" in res.stdout
    assert not (env["proj"] / ".agents").exists()
    assert not (env["proj"] / "skills-applied.json").exists()


def test_apply_dry_run_reports_existing_skill_without_replacing_it(env):
    hand = make_skill(env["proj"] / ".agents" / "skills", "tdd")

    res = skctl(env, "apply", "mypreset", "--dry-run")

    assert res.returncode == 0, res.stderr
    assert "Would keep existing .agents/skills/tdd" in res.stdout
    assert (hand / "SKILL.md").is_file()


def test_apply_resync_dry_run_does_not_restore_missing_links(env):
    assert skctl(env, "apply", "mypreset", "-s", "tdd").returncode == 0
    link = env["proj"] / ".agents" / "skills" / "tdd"
    link.unlink()

    res = skctl(env, "apply", "--resync", "--dry-run")

    assert res.returncode == 0, res.stderr
    assert "Would link .agents/skills/tdd" in res.stdout
    assert not link.exists()


def test_apply_dry_run_does_not_reindex_a_project_record(env):
    (env["proj"] / "skills-applied.json").write_text(
        json.dumps(
            {
                "version": 1,
                "presets": {
                    "mypreset": {
                        "skills": ["tdd"],
                        "targets": [".agents/skills"],
                        "mode": "symlink",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    res = skctl(env, "apply", "mypreset", "--dry-run")

    assert res.returncode == 0, res.stderr
    assert not (env["home"] / ".skill-ctl" / "applied.json").exists()
    assert not (env["home"] / ".skill-ctl" / "config.yaml").exists()


def test_apply_rejects_unknown_skill(env):
    res = skctl(env, "apply", "mypreset", "-s", "nope")
    assert res.returncode == 1
    assert "nope" in res.stdout + res.stderr


def test_apply_interactive_selection(env):
    proc = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "apply", "mypreset", "-s"],
        cwd=env["proj"],
        env=env["env"],
        input="1\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    # The menu is numbered over sorted names: 1) review, 2) tdd
    assert (env["proj"] / ".agents" / "skills" / "review").is_symlink()
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()


def run_in_pty(env, args, drain_seconds=3.0, script=None):
    """Run skctl with a real tty on stdin, the case a subprocess pipe can't cover.

    `interactive` in apply() is `sys.stdin.isatty()`, which is always False for a
    plain subprocess pipe - so a bug that only shows up with a real terminal, like
    asking for skills it should not, would pass every pipe-based test and still
    reach a real user. This is skipped where pty is unavailable (Windows CI).

    `script`, if given, is a list of (delay, bytes) writes sent while draining -
    answers to prompts that only appear once the process is far enough along.
    The returned text has ANSI escapes stripped: rich colours a quoted name (like
    `'newpreset'`) apart from the words around it, so a plain phrase spanning both
    is only one contiguous string once those codes are gone.
    """
    if sys.platform == "win32":
        pytest.skip("pty is unavailable on Windows")

    import pty
    import re
    import select

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(env["proj"])
        os.execve(sys.executable, [sys.executable, "-m", "skill_ctl.cli", *args], env["env"])

    chunks = []
    end = time.time() + drain_seconds
    for delay, data in script or []:
        deadline = time.time() + delay
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunks.append(os.read(fd, 65536).decode(errors="replace"))
                except OSError:
                    break
        os.write(fd, data)
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data.decode(errors="replace"))
    os.waitpid(pid, 0)
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", "".join(chunks))


@pytest.mark.skipif(sys.platform == "win32", reason="pty is unix-only")
def test_naming_a_preset_in_a_real_terminal_applies_everything_with_no_question(env):
    """`skctl apply mypreset` from an actual terminal must not open a picker."""
    out = run_in_pty(env, ["apply", "mypreset"], script=[(0.1, b"\n")])

    assert "Skills to apply" not in out, "naming the preset must skip the question"
    assert "Preset to apply" not in out, "a preset was already named"
    for name in ("tdd", "review"):
        link = env["proj"] / ".agents" / "skills" / name
        assert link.is_symlink(), f"{name} was not applied: {out}"


@pytest.mark.skipif(sys.platform == "win32", reason="pty is unix-only")
def test_pick_still_asks_even_with_a_named_preset(env, tmp_path):
    """--pick is how you ask for the picker back once a preset is named."""
    bin_dir = None
    # A stub fzf that always picks 'review', so the run finishes without input.
    bin_dir = tmp_path / "fzfbin"
    bin_dir.mkdir()
    script = bin_dir / "fzf"
    script.write_text(
        "#!/bin/sh\ncat | grep 'review'\nexit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    env_with_fzf = dict(
        env, env=dict(env["env"], PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    )

    out = run_in_pty(env_with_fzf, ["apply", "mypreset", "--pick"], script=[(0.1, b"\n")])

    assert (env["proj"] / ".agents" / "skills" / "review").is_symlink(), out
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()


def fake_fzf(tmp_path, picks, exit_code=0):
    """A stand-in for fzf: reads the candidate lines, echoes back the chosen ones."""
    bin_dir = tmp_path / "fzfbin"
    bin_dir.mkdir(exist_ok=True)
    wanted = " ".join(f"'{name}'" for name in picks)
    if os.name == "nt":
        py_script = bin_dir / "fzf.py"
        py_code = (
            f"import sys\n"
            f"picks = {list(picks)!r}\n"
            f"lines = sys.stdin.readlines()\n"
            f"for p in picks:\n"
            f"    for l in lines:\n"
            f"        if p in l:\n"
            f"            sys.stdout.write(l)\n"
            f"sys.exit({exit_code})\n"
        )
        py_script.write_text(py_code, encoding="utf-8")
        script = bin_dir / "fzf.cmd"
        script.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0fzf.py"\r\nexit /b %errorlevel%\r\n',
            encoding="utf-8",
        )
    else:
        script = bin_dir / "fzf"
        script.write_text(
            "#!/bin/sh\n"
            "input=$(cat)\n"
            f"for want in {wanted}; do\n"
            '  printf "%s\\n" "$input" | grep "$want"\n'
            "done\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return bin_dir


def with_fzf(env, bin_dir):
    return dict(env["env"], PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def force_fzf(env):
    """fzf is only used automatically where there is a terminal, which tests lack."""
    cfg = env["home"] / ".skill-ctl" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("prompts:\n  picker: fzf\n", encoding="utf-8")


def force_numbers(env):
    """The reverse of force_fzf: a real terminal with real fzf on PATH would
    otherwise reach for it in "auto" mode, and a test's scripted keystrokes for
    the numbered fallback mean nothing to fzf's own UI."""
    cfg = env["home"] / ".skill-ctl" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("prompts:\n  picker: numbers\n", encoding="utf-8")


def test_presets_browse_cancel_does_not_list_presets(env, tmp_path):
    force_fzf(env)
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "presets"],
        cwd=env["proj"],
        env=with_fzf(env, fake_fzf(tmp_path, [], exit_code=130)),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "Presets in" not in res.stdout


def test_all_presets_offers_every_skill_wherever_it_is_filed(env, tmp_path):
    """0 at the preset prompt, or --all-presets: no preset to name first."""
    force_fzf(env)
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "apply", "--all-presets"],
        cwd=env["proj"],
        env=with_fzf(env, fake_fzf(tmp_path, ["review", "tdd"])),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    # 'review' only exists in mypreset, 'tdd' in both; fzf saw them all at once.
    record = json.loads((env["proj"] / "skills-applied.json").read_text())["presets"]
    assert "mypreset" in record
    assert sorted(record["mypreset"]["skills"]) == ["review", "tdd"]


def test_all_presets_applies_each_presets_share(env, tmp_path):
    """A selection spanning presets goes through one ordinary apply per preset."""
    force_fzf(env)
    solo = env["presets"] / "solo" / ".agents" / "skills"
    make_skill(solo, "only-here")

    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "apply", "-A"],
        cwd=env["proj"],
        env=with_fzf(env, fake_fzf(tmp_path, ["review", "only-here"])),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    record = json.loads((env["proj"] / "skills-applied.json").read_text())["presets"]
    assert sorted(record) == ["mypreset", "solo"]
    assert record["solo"]["skills"] == ["only-here"]
    assert (env["proj"] / ".agents" / "skills" / "only-here").is_symlink()


def test_all_presets_picking_nothing_applies_nothing(env, tmp_path):
    force_fzf(env)
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "apply", "-A"],
        cwd=env["proj"],
        env=with_fzf(env, fake_fzf(tmp_path, [], exit_code=130)),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "Nothing picked" in " ".join(res.stdout.split())
    assert not (env["proj"] / ".agents").exists()


def test_apply_pick_chooses_the_skills_in_fzf(env, tmp_path):
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "apply", "mypreset", "--pick"],
        cwd=env["proj"],
        env=with_fzf(env, fake_fzf(tmp_path, ["review"])),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert (env["proj"] / ".agents" / "skills" / "review").is_symlink()
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()


def test_apply_pick_cancelled_applies_nothing(env, tmp_path):
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "apply", "mypreset", "-i"],
        cwd=env["proj"],
        env=with_fzf(env, fake_fzf(tmp_path, [], exit_code=130)),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "Nothing picked" in " ".join(res.stdout.split())
    assert not (env["proj"] / ".agents").exists()
    assert not (env["proj"] / "skills-applied.json").exists()


def test_picker_numbers_setting_keeps_the_numbered_list(env, tmp_path):
    """fzf installed and asked for by config is still overridable back to numbers."""
    cfg = env["home"] / ".skill-ctl" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("prompts:\n  picker: numbers\n", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "apply", "mypreset", "-s"],
        cwd=env["proj"],
        env=with_fzf(env, fake_fzf(tmp_path, ["review"])),
        input="2\n",
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    # The numbered menu is sorted: 1) review, 2) tdd. fzf would have picked review.
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()
    assert not (env["proj"] / ".agents" / "skills" / "review").exists()


def test_picker_fzf_setting_is_used_without_the_flag(env, tmp_path):
    cfg = env["home"] / ".skill-ctl" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("prompts:\n  picker: fzf\n", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "apply", "mypreset", "-s"],
        cwd=env["proj"],
        env=with_fzf(env, fake_fzf(tmp_path, ["review"])),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert (env["proj"] / ".agents" / "skills" / "review").is_symlink()


def test_bare_apply_is_not_interactive_without_a_tty(env):
    """Scripts and CI must keep the old behaviour: default preset, all skills."""
    res = skctl(env, "apply")
    assert res.returncode == 0, res.stderr
    assert "Preset to apply" not in res.stdout


def test_bare_apply_respects_ask_apply_false(env):
    cfg = env["home"] / ".skill-ctl" / "config.yaml"
    skctl(env, "presets")  # materialise the config file
    cfg.write_text("default_preset: mypreset\nprompts:\n  ask_apply: false\n", encoding="utf-8")
    res = skctl(env, "apply")
    assert res.returncode == 0, res.stderr
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="pty is unix-only")
def test_bare_apply_prompts_for_specific_skill_or_preset(env, tmp_path):
    """Running bare `apply` in a terminal prompts: destination, 1) Specific skill(s), 2) An entire preset."""
    out = run_in_pty(
        env,
        ["apply"],
        drain_seconds=4.0,
        script=[(0.2, b"1\n"), (0.4, b"2\n"), (0.6, b"1\n"), (0.8, b"\n")],
    )
    assert "Where would you like to apply" in out
    assert "What would you like to apply" in out
    assert "Specific skill(s)" in out
    assert "An entire preset" in out
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()
    assert (env["proj"] / ".agents" / "skills" / "review").is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="pty is unix-only")
def test_bare_apply_choice_1_picks_specific_skill(env, tmp_path):
    """Picking '1' in bare apply searches and applies specific skills."""
    bin_dir = tmp_path / "fzfbin"
    bin_dir.mkdir()
    script = bin_dir / "fzf"
    script.write_text("#!/bin/sh\ncat | grep 'review'\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    env_with_fzf = dict(
        env, env=dict(env["env"], PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    )

    out = run_in_pty(
        env_with_fzf,
        ["apply"],
        drain_seconds=4.0,
        script=[(0.2, b"1\n"), (0.4, b"1\n"), (0.8, b"\n")],
    )
    assert "Where would you like to apply" in out
    assert "What would you like to apply" in out
    assert (env["proj"] / ".agents" / "skills" / "review").is_symlink()
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="pty is unix-only")
@pytest.mark.parametrize("skill_count", [2, 26])
def test_unapply_confirmed_in_a_real_terminal(env, skill_count):
    for index in range(skill_count - 2):
        make_skill(env["presets"] / "mypreset" / ".agents" / "skills", f"extra-{index}")
    res = skctl(env, "apply", "mypreset")
    assert res.returncode == 0, res.stderr

    out = run_in_pty(
        env,
        ["unapply", "mypreset"],
        script=[(0.2, b"1\n"), (0.2, b"y\n")],
    )

    assert "Remove preset 'mypreset'" in out
    assert f"Unapplied preset 'mypreset' ({skill_count * 2} removed, 0 skipped)." in out
    assert "TypeError" not in out
    assert "lost sys.stderr" not in out
    assert not (env["proj"] / ".agents").exists()
    assert not (env["proj"] / ".claude").exists()


def test_unapply_removes_only_its_own_links(env):
    assert skctl(env, "apply", "mypreset").returncode == 0

    # A skill the user installed directly: a real directory, same name as a preset skill.
    hand = env["proj"] / ".claude" / "skills" / "tdd"
    hand.unlink()  # drop the symlink apply just made
    make_skill(env["proj"] / ".claude" / "skills", "tdd")
    (hand / "SKILL.md").write_text("# hand-installed\n", encoding="utf-8")

    # A link owned by a *different* preset, also sharing a name.
    other = env["proj"] / ".agents" / "skills" / "review"
    other.unlink()
    other.symlink_to(env["presets"] / "otherp" / ".agents" / "skills" / "tdd")

    res = skctl(env, "unapply", "mypreset")
    assert res.returncode == 0, res.stderr

    # Neither of the foreign entries may be touched.
    assert hand.is_dir() and "hand-installed" in (hand / "SKILL.md").read_text()
    assert other.is_symlink()
    assert "Skipping" in res.stdout

    # The genuine preset links are gone.
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()
    assert not (env["proj"] / ".claude" / "skills" / "review").exists()


def test_unapply_force_removes_real_directories(env):
    assert skctl(env, "apply", "mypreset", "--copy").returncode == 0
    copied = env["proj"] / ".agents" / "skills" / "tdd"
    assert copied.is_dir() and not copied.is_symlink()

    assert skctl(env, "unapply", "mypreset").returncode == 0
    assert copied.is_dir(), "plain unapply must not delete copied skills"

    assert skctl(env, "unapply", "mypreset", "--force").returncode == 0
    assert not copied.exists()


def test_apply_keeps_a_real_directory_it_does_not_own(env):
    """A skill installed or edited straight into the project is not apply's to delete."""
    hand = make_skill(env["proj"] / ".agents" / "skills", "tdd")
    (hand / "SKILL.md").write_text("# hand-installed\n", encoding="utf-8")

    res = skctl(env, "apply", "mypreset")
    assert res.returncode == 0, res.stderr
    assert hand.is_dir() and not hand.is_symlink()
    assert "hand-installed" in (hand / "SKILL.md").read_text()
    assert "Kept existing" in " ".join(res.stdout.split())

    # The other target directory had nothing in the way, so it was linked there.
    assert (env["proj"] / ".claude" / "skills" / "tdd").is_symlink()


def test_apply_does_not_record_a_skill_it_refused_to_place(env):
    """Recording it would let a later unapply or --resync claim someone else's work."""
    hand = make_skill(env["proj"] / ".agents" / "skills", "tdd")
    (hand / "SKILL.md").write_text("# hand-installed\n", encoding="utf-8")

    assert skctl(env, "apply", "mypreset", "-a", "universal").returncode == 0
    record = json.loads((env["proj"] / "skills-applied.json").read_text(encoding="utf-8"))
    assert record["presets"]["mypreset"]["skills"] == ["review"]

    assert skctl(env, "unapply", "mypreset").returncode == 0
    assert "hand-installed" in (hand / "SKILL.md").read_text()


def test_apply_force_replaces_a_real_directory(env):
    hand = make_skill(env["proj"] / ".agents" / "skills", "tdd")
    (hand / "SKILL.md").write_text("# hand-installed\n", encoding="utf-8")

    assert skctl(env, "apply", "mypreset", "--force").returncode == 0
    assert hand.is_symlink()


def test_apply_copy_refreshes_its_own_copy(env):
    """Copies apply made are its to overwrite, or a second --copy would do nothing."""
    assert skctl(env, "apply", "mypreset", "--copy").returncode == 0
    (env["presets"] / "mypreset" / ".agents" / "skills" / "tdd" / "SKILL.md").write_text(
        "# updated\n", encoding="utf-8"
    )

    res = skctl(env, "apply", "mypreset", "--copy")
    assert res.returncode == 0, res.stderr
    assert "Kept existing" not in " ".join(res.stdout.split())
    copied = env["proj"] / ".agents" / "skills" / "tdd" / "SKILL.md"
    assert copied.read_text() == "# updated\n"


def test_unapply_force_reaches_a_copy_dropped_from_the_preset(env):
    """Only the record still names it, so without it the directory is unreachable."""
    assert skctl(env, "apply", "mypreset", "--copy").returncode == 0
    shutil.rmtree(env["presets"] / "mypreset" / ".agents" / "skills" / "review")
    stale = env["proj"] / ".agents" / "skills" / "review"
    assert stale.is_dir()

    res = skctl(env, "unapply", "mypreset")
    assert res.returncode == 0, res.stderr
    assert stale.is_dir(), "a copy still needs --force"
    assert "a copy from 'mypreset'" in " ".join(res.stdout.split())

    assert skctl(env, "unapply", "mypreset", "--force").returncode == 0
    assert not stale.exists()


def test_bare_unapply_on_an_unused_project_is_not_an_error(env, tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "unapply"],
        cwd=clean,
        env=env["env"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr


def test_default_preset_exists_on_a_fresh_install(tmp_path):
    home = tmp_path / "home"
    fresh = {
        "env": dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO_ROOT)),
        "proj": tmp_path,
    }
    res = skctl(fresh, "presets")
    assert res.returncode == 0, res.stderr
    assert (home / ".skill-ctl" / "presets" / "default").is_dir()
    assert "default" in res.stdout


def test_rm_default_preset_leaves_an_empty_one(env):
    assert skctl(env, "presets", "delete", "default").returncode == 0
    assert (env["presets"] / "default").is_dir(), "default preset must survive as empty"

    # apply must still work rather than erroring on a dangling default.
    res = skctl(env, "apply")
    assert res.returncode == 0, res.stderr
    assert "no installed skills" in res.stdout


def test_rm_named_preset_is_not_recreated(env):
    assert skctl(env, "presets", "delete", "otherp").returncode == 0
    assert not (env["presets"] / "otherp").exists()


def test_create_without_a_terminal_asks_nothing(env):
    """A script creating presets in a loop has no tty to answer through."""
    res = skctl(env, "presets", "create", "newpreset")
    assert res.returncode == 0, res.stderr
    assert "Add skills to" not in res.stdout


def test_create_from_agents_path_copies_skills(env):
    make_skill(env["proj"] / ".agents" / "skills", "local-skill")

    res = skctl(env, "presets", "create", "from-project", "--from", str(env["proj"] / ".agents"))

    assert res.returncode == 0, res.stderr
    copied = env["presets"] / "from-project" / "local-skill" / "SKILL.md"
    assert copied.read_text(encoding="utf-8") == "# local-skill\n"


def test_clone_copies_a_preset_without_changing_its_source(env):
    res = skctl(env, "presets", "clone", "mypreset", "research-paper")

    assert res.returncode == 0, res.stderr
    copied = env["presets"] / "research-paper" / ".agents" / "skills" / "tdd" / "SKILL.md"
    assert copied.read_text(encoding="utf-8") == "# tdd\n"
    assert (env["presets"] / "mypreset" / ".agents" / "skills" / "tdd").is_dir()


def test_combine_flattens_skills_and_dry_run_does_not_write(env):
    preview = skctl(env, "presets", "combine", "dev", "mypreset", "otherp", "--dry-run")

    assert preview.returncode == 0, preview.stderr
    assert "Would create preset 'dev'" in preview.stdout
    assert not (env["presets"] / "dev").exists()

    res = skctl(env, "presets", "combine", "dev", "mypreset", "otherp")

    assert res.returncode == 0, res.stderr
    assert (env["presets"] / "dev" / "tdd" / "SKILL.md").is_file()
    assert (env["presets"] / "dev" / "review" / "SKILL.md").is_file()


def test_combine_refuses_existing_destination_without_replace(env):
    assert skctl(env, "presets", "combine", "dev", "mypreset", "otherp").returncode == 0

    res = skctl(env, "presets", "combine", "dev", "mypreset", "otherp")

    assert res.returncode == 1
    assert "already exists" in res.stdout + res.stderr


def test_clone_records_its_source_in_preset_history(env):
    assert skctl(env, "presets", "clone", "mypreset", "research-paper").returncode == 0

    res = skctl(env, "presets", "history", "research-paper")

    assert res.returncode == 0, res.stderr
    assert "clone" in res.stdout
    assert "mypreset" in res.stdout


def test_combine_records_all_source_presets_in_history(env):
    assert skctl(env, "presets", "combine", "dev", "mypreset", "otherp").returncode == 0

    history = json.loads((env["presets"] / "dev" / ".skctl.json").read_text(encoding="utf-8"))

    assert history["events"][0]["action"] == "combine"
    assert history["events"][0]["sources"] == ["mypreset", "otherp"]


def test_preset_browser_preview_shows_combine_origin(env):
    from skill_ctl.presets import preset_preview

    assert skctl(env, "presets", "combine", "dev", "mypreset", "otherp").returncode == 0

    preview = preset_preview(env["presets"] / "dev", [])

    assert "History:" in preview
    assert "combine" in preview
    assert "mypreset, otherp" in preview


@pytest.mark.skipif(sys.platform == "win32", reason="pty is unix-only")
def test_clone_offers_to_add_more_skills_after_creation(env):
    make_skill(env["presets"] / "extra" / ".agents" / "skills", "unique")

    out = run_in_pty(
        env, ["presets", "clone", "mypreset", "research-paper"], script=[(0.8, b"3\n")]
    )

    assert "Add more skills to 'research-paper'?" in out
    assert "No, Done" in out


def test_create_offers_nothing_when_no_other_preset_has_skills(env, tmp_path):
    """skills.sh remains useful even when nothing is installed locally."""
    home = tmp_path / "emptyhome"
    empty_env = dict(env, home=home, env=dict(env["env"], HOME=str(home), USERPROFILE=str(home)))
    out = run_in_pty(empty_env, ["presets", "create", "newpreset"], script=[(0.8, b"3\n")])
    assert "Add more skills to 'newpreset'?" in out, out


def test_create_offers_and_copies_via_the_numbered_list(env):
    """Off fzf (`picker: numbers`), the same flow falls back to the numbered list."""
    force_numbers(env)
    out = run_in_pty(
        env,
        ["presets", "create", "newpreset"],
        script=[
            (0.8, b"1\n"),  # Pick from installed presets or global folders.
            (0.8, b"1\n"),  # the list, sorted by 'preset/skill': mypreset/review first
        ],
    )

    assert "Created preset 'newpreset'" in out, out
    assert "Added 1 skill to 'newpreset': review" in out, out
    copied = env["presets"] / "newpreset" / ".agents" / "skills" / "review"
    assert copied.is_dir() and not copied.is_symlink(), "a copy, not a link into the other preset"
    assert (copied / "SKILL.md").exists()


def test_create_declining_leaves_the_preset_empty(env):
    out = run_in_pty(env, ["presets", "create", "newpreset"], script=[(0.8, b"3\n")])
    assert "Created preset 'newpreset'" in out, out
    skills_dir = env["presets"] / "newpreset" / ".agents" / "skills"
    assert not skills_dir.exists() or not any(skills_dir.iterdir())


def test_create_picking_nothing_in_fzf_leaves_the_preset_empty(env, tmp_path):
    """Esc in fzf is the actual "pick nothing" path; the numbered fallback's blank
    line means "all", so this only exists through the picker."""
    force_fzf(env)
    env["env"] = with_fzf(env, fake_fzf(tmp_path, [], exit_code=130))
    out = run_in_pty(env, ["presets", "create", "newpreset"], script=[(0.8, b"1\n")])

    assert "Nothing picked" in out, out
    skills_dir = env["presets"] / "newpreset" / ".agents" / "skills"
    assert not skills_dir.exists() or not any(skills_dir.iterdir())


def test_create_on_an_existing_preset_does_not_ask_again(env):
    """Re-running `create` on a preset that already has skills must not reopen this."""
    out = run_in_pty(env, ["presets", "create", "mypreset"])
    assert "Add skills to" not in out, out


def test_add_picker_excludes_a_skill_already_in_the_target(env, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])

    preset_dir = env["presets"] / "newpreset"
    dst = preset_dir / ".agents" / "skills" / "review"
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("# hand-placed\n", encoding="utf-8")

    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod.Prompt, "ask", lambda *a, **k: "1")
    monkeypatch.setattr(presets_mod, "wants_picker", lambda config: False)
    seen = {}

    def choose(labels):
        seen["labels"] = labels
        return [labels[0]]

    monkeypatch.setattr(presets_mod, "prompt_skills", choose)

    presets_mod.offer_add_after_create("newpreset", preset_dir, {"prompts": {}})

    assert not any(label.endswith("/review") for label in seen["labels"])
    assert (dst / "SKILL.md").read_text() == "# hand-placed\n"


def test_add_without_package_searches_remote(env, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(skills_mod.sys.stdin, "isatty", lambda: True)
    seen = {}

    def fake_search(**kwargs):
        seen["search"] = kwargs

    monkeypatch.setattr("skill_ctl.search.search", fake_search)

    skills_mod.add(package=None, preset="mypreset")

    assert seen["search"]["preset"] == "mypreset"
    assert seen["search"]["remote_only"] is True


def test_remove_without_skill_uses_preset_picker(env, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(skills_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(skills_mod, "wants_picker", lambda config: True)
    monkeypatch.setattr(
        skills_mod,
        "pick",
        lambda rows, header=None, action="apply": [row for row in rows if row["skill"] == "review"],
    )
    seen = {}
    monkeypatch.setattr(
        skills_mod,
        "run_npx_skills",
        lambda args, cwd=None: seen.update(args=args, cwd=cwd) or 0,
    )

    skills_mod.remove(skill=None, preset="mypreset", yes=True)

    assert seen["args"] == ["remove", "review", "-y"]
    assert seen["cwd"] == str(env["presets"] / "mypreset")


def test_create_can_copy_an_untracked_global_skill(env, monkeypatch, capsys):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    global_skill = make_skill(env["home"] / ".agents" / "skills", "global-only")
    monkeypatch.setenv("HOME", str(env["home"]))
    monkeypatch.setenv("USERPROFILE", str(env["home"]))
    preset_dir = env["presets"] / "newpreset"
    preset_dir.mkdir(parents=True)
    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod.Prompt, "ask", lambda *a, **k: "1")
    monkeypatch.setattr(presets_mod, "wants_picker", lambda config: True)
    monkeypatch.setattr(
        presets_mod,
        "pick",
        lambda rows, header=None: [row for row in rows if row["path"] == str(global_skill)],
    )

    presets_mod.offer_add_after_create(
        "newpreset",
        preset_dir,
        {"prompts": {}, "global_skill_dirs": [str(global_skill.parent)]},
    )

    copied = preset_dir / ".agents" / "skills" / "global-only"
    assert (copied / "SKILL.md").is_file()
    warning = capsys.readouterr().out
    assert "cannot" in warning and "update: global-only" in " ".join(warning.split())


def test_create_reinstalls_a_tracked_global_skill(env, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    global_skill = make_skill(env["home"] / ".agents" / "skills", "tracked")
    monkeypatch.setenv("HOME", str(env["home"]))
    monkeypatch.setenv("USERPROFILE", str(env["home"]))
    lock = env["home"] / ".agents" / ".skill-lock.json"
    lock.write_text(
        json.dumps({"version": 3, "skills": {"tracked": {"source": "owner/package"}}}),
        encoding="utf-8",
    )
    preset_dir = env["presets"] / "newpreset"
    preset_dir.mkdir(parents=True)
    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod.Prompt, "ask", lambda *a, **k: "1")
    monkeypatch.setattr(presets_mod, "wants_picker", lambda config: True)
    monkeypatch.setattr(
        presets_mod,
        "pick",
        lambda rows, header=None: [row for row in rows if row["path"] == str(global_skill)],
    )
    seen = {}

    def fake_npx(args, cwd=None):
        seen["args"] = args
        make_skill(preset_dir / ".agents" / "skills", "tracked")
        (preset_dir / "skills-lock.json").write_text('{"version": 1, "skills": {}}\n')
        return 0

    monkeypatch.setattr(presets_mod, "run_npx_skills", fake_npx)

    presets_mod.offer_add_after_create(
        "newpreset",
        preset_dir,
        {"prompts": {}, "global_skill_dirs": [str(global_skill.parent)]},
    )

    assert seen["args"] == [
        "add",
        "owner/package",
        "--skill",
        "tracked",
        "--agent",
        "universal",
        "-y",
    ]
    assert (preset_dir / "skills-lock.json").is_file()


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["add", "x", "--preset"], ["add", "x", "--preset", "default"]),
        (["add", "x", "-P"], ["add", "x", "-P", "default"]),
        (["add", "x", "--preset", "my-preset"], ["add", "x", "--preset", "my-preset"]),
        (["add", "x", "-p"], ["add", "x", "-p", "."]),
        (["preset", "create", "foo"], ["presets", "create", "foo"]),
        (["rm", "tdd"], ["remove", "tdd"]),
        (["rm"], ["remove"]),
        (["add", "npx", "skills", "add", "y", "-p"], ["add", "y", "-p", "."]),
    ],
)
def test_preprocess_args(argv, expected):
    """Bare --preset resolves through load_config, which only ever reads."""
    from skill_ctl.cli import preprocess_args

    assert preprocess_args(argv) == expected


def test_unapply_removes_emptied_agent_dirs(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    assert (env["proj"] / ".claude" / "skills").is_dir()

    res = skctl(env, "unapply", "mypreset")
    assert res.returncode == 0, res.stderr
    assert not (env["proj"] / ".claude").exists(), "empty agent dir left behind"
    assert not (env["proj"] / ".agents").exists()
    assert env["proj"].is_dir(), "must never remove the project directory itself"


def test_unapply_keeps_agent_dirs_that_still_hold_skills(env):
    assert skctl(env, "apply", "mypreset").returncode == 0
    keep = env["proj"] / ".claude" / "skills" / "unrelated"
    make_skill(env["proj"] / ".claude" / "skills", "unrelated")

    assert skctl(env, "unapply", "mypreset").returncode == 0
    assert keep.is_dir(), "a dir with someone else's skill must survive"


def test_unapply_drops_stale_lockfile_entries(env):
    lock = env["proj"] / "skills-lock.json"
    lock.write_text(
        '{"version": 1, "skills": {"tdd": {"source": "x"}, "other": {"source": "y"}}}\n',
        encoding="utf-8",
    )
    assert skctl(env, "apply", "mypreset").returncode == 0
    assert skctl(env, "unapply", "mypreset").returncode == 0

    data = json.loads(lock.read_text(encoding="utf-8"))
    assert "tdd" not in data["skills"], "removed skill still recorded"
    assert "other" in data["skills"], "must not touch entries we did not remove"


def test_prune_lockfile_only_touches_what_it_removed(tmp_path):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl.presets import prune_lockfile

    lock = tmp_path / "skills-lock.json"

    # Nothing stale: the file must survive untouched, even when already empty.
    lock.write_text('{"version": 1, "skills": {}}', encoding="utf-8")
    assert prune_lockfile(tmp_path, {"tdd"}) == 0
    assert lock.exists(), "deleted a lockfile it found nothing stale in"

    # Other top-level keys must survive.
    lock.write_text('{"version": 1, "custom": "keep", "skills": {"tdd": {}}}', encoding="utf-8")
    assert prune_lockfile(tmp_path, {"tdd"}) == 1
    assert json.loads(lock.read_text())["custom"] == "keep"

    # Only when we emptied it and nothing else is in there does it go.
    lock.write_text('{"version": 1, "skills": {"tdd": {}}}', encoding="utf-8")
    assert prune_lockfile(tmp_path, {"tdd"}) == 1
    assert not lock.exists()


def test_invalid_default_preset_falls_back(tmp_path):
    """An empty `default_preset:` is None in YAML and must not make a preset named 'None'."""
    home = tmp_path / "home"
    (home / ".skill-ctl").mkdir(parents=True)
    (home / ".skill-ctl" / "config.yaml").write_text("default_preset:\n", encoding="utf-8")

    res = subprocess.run(
        [
            sys.executable,
            "-c",
            "from skill_ctl.config import load_config; print(load_config()['default_preset'])",
        ],
        env=dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO_ROOT)),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().endswith("default")
    assert not (home / ".skill-ctl" / "presets" / "None").exists()


def test_messages_do_not_eat_square_brackets(capsys):
    """Skill names and paths are not rich markup: '[dev]' must survive, '[/x]' must not raise."""
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl.ui import print_success, print_warn

    print_success("installed .[dev] extra")
    print_warn("stray [/nope] tag")
    out = capsys.readouterr().out
    assert "[dev]" in out
    assert "[/nope]" in out


def test_npx_args_target_skills_dir_and_repeat_skill_flags(env, monkeypatch):
    """`npx skills add` ignores skills listed in a skills-lock.json beside them, and
    matches nothing for a comma list - so point at the skills dir, one flag per name."""
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    preset = env["presets"] / "mypreset"
    (preset / "skills-lock.json").write_text('{"version": 1, "skills": {"tdd": {}}}\n')

    captured = {}

    def fake_npx(args, cwd=None):
        captured["args"] = args
        return 0

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod, "run_npx_skills", fake_npx)
    monkeypatch.setattr(
        presets_mod,
        "load_config",
        lambda: {
            "default_preset": "mypreset",
            "apply_targets": [".agents/skills"],
            "apply_mode": "symlink",
            "prompts": {"ask_apply": False},
            "custom_agents": {},
        },
    )
    monkeypatch.chdir(env["proj"])

    with pytest.raises(SystemExit):
        presets_mod.apply("mypreset", npx=True, skill="tdd,review")

    args = captured["args"]
    assert args[1].replace("\\", "/").endswith(".agents/skills"), (
        f"must target the skills dir, got {args[1]}"
    )
    assert "skills-lock.json" not in args[1]
    assert args.count("--skill") == 2, f"one --skill per name, got {args}"
    assert not any("," in a for a in args), "comma-joined names match nothing upstream"

    # Without -s, npx does its own selecting, so send no filter at all.
    captured.clear()
    with pytest.raises(SystemExit):
        presets_mod.apply("mypreset", npx=True)
    assert "--skill" not in captured["args"], captured["args"]


def npx_stub(env, presets_mod, monkeypatch, installs, kind="copy"):
    """Stand in for `npx skills add`, which picks its own skills and directories."""
    from skill_ctl import registry

    def fake_npx(args, cwd=None):
        for rel_dir, name in installs:
            dst = env["proj"] / rel_dir / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or dst.exists():
                shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
            if kind == "copy":
                shutil.copytree(env["presets"] / "mypreset" / ".agents" / "skills" / name, dst)
            else:
                dst.symlink_to(env["presets"] / "mypreset" / ".agents" / "skills" / name)
        return 0

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod, "run_npx_skills", fake_npx)
    monkeypatch.setattr(registry, "APPLIED_FILE", env["home"] / ".skill-ctl" / "applied.json")
    monkeypatch.setattr(
        presets_mod,
        "load_config",
        lambda: {
            "default_preset": "mypreset",
            "apply_targets": [".agents/skills"],
            "apply_mode": "symlink",
            "prompts": {"ask_apply": False},
            "custom_agents": {},
        },
    )
    monkeypatch.chdir(env["proj"])


def record(env) -> dict:
    return json.loads((env["proj"] / "skills-applied.json").read_text(encoding="utf-8"))["presets"]


def test_npx_apply_records_what_landed(env, monkeypatch):
    """Otherwise unapply, --resync and doctor never learn the project has them."""
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    npx_stub(env, presets_mod, monkeypatch, [(".agents/skills", "tdd")])
    with pytest.raises(SystemExit):
        presets_mod.apply("mypreset", npx=True)

    entry = record(env)["mypreset"]
    assert entry["skills"] == ["tdd"], "npx installed tdd and nothing else"
    assert entry["mode"] == "copy", "npx copied, so clearing it needs --force"
    assert entry["targets"] == [".agents/skills"]


def test_npx_apply_finds_an_agent_directory_skctl_does_not_map(env, monkeypatch):
    """`npx skills --agent goose` installs into .goose/skills; upstream knows a dozen
    agents skctl does not, so what is on disk decides, not a hardcoded list."""
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    npx_stub(env, presets_mod, monkeypatch, [(".goose/skills", "review")])
    with pytest.raises(SystemExit):
        presets_mod.apply("mypreset", npx=True, agent="goose")

    entry = record(env)["mypreset"]
    assert entry["skills"] == ["review"]
    assert entry["targets"] == [".goose/skills"], "the directory npx chose must be recorded"

    # And it is reachable again: nothing else in the project names it.
    presets_mod.unapply("mypreset", force=True)
    assert not (env["proj"] / ".goose").exists()


def test_npx_apply_does_not_claim_a_skill_that_was_already_there(env, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    mine = make_skill(env["proj"] / ".agents" / "skills", "review")
    (mine / "SKILL.md").write_text("# hand-installed\n", encoding="utf-8")

    npx_stub(env, presets_mod, monkeypatch, [(".agents/skills", "tdd")])
    with pytest.raises(SystemExit):
        presets_mod.apply("mypreset", npx=True)

    assert record(env)["mypreset"]["skills"] == ["tdd"], "review was not npx's doing"


def test_npx_apply_notices_a_symlink_it_replaced_with_a_copy(env, monkeypatch):
    """The case that left a stale 'symlink' record: apply, then apply --npx."""
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    npx_stub(env, presets_mod, monkeypatch, [(".agents/skills", "tdd")])
    presets_mod.link_skills(
        env["proj"],
        "mypreset",
        {"tdd": env["presets"] / "mypreset" / ".agents" / "skills" / "tdd"},
        [".agents/skills"],
        False,
    )
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()

    with pytest.raises(SystemExit):
        presets_mod.apply("mypreset", npx=True)

    entry = record(env)["mypreset"]
    assert entry["mode"] == "copy", "the record must follow what is now on disk"
    assert entry["skills"] == ["tdd"]


def test_missing_npx_reports_clearly(env):
    """On Windows npx is npx.cmd; resolving via PATH must fail loudly, not silently."""
    broken = dict(env["env"], PATH="/nonexistent")
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "list"],
        cwd=env["proj"],
        env=broken,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert "npx" in res.stdout + res.stderr


def test_apply_falls_back_to_copy_when_symlinks_fail(env, monkeypatch):
    """Windows refuses symlinks without Developer Mode; apply must still work."""
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import presets as presets_mod

    def no_symlinks(self, target, target_is_directory=False):
        raise OSError(1314, "A required privilege is not held by the client")

    from skill_ctl import registry

    monkeypatch.setattr(Path, "symlink_to", no_symlinks)
    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    # apply() records what it did, and this test calls it in-process: without this
    # the machine-wide index in the real HOME collects an entry under /tmp.
    monkeypatch.setattr(registry, "APPLIED_FILE", env["home"] / ".skill-ctl" / "applied.json")
    # Stub the config so the test never reads or creates anything in the real HOME.
    monkeypatch.setattr(
        presets_mod,
        "load_config",
        lambda: {
            "default_preset": "mypreset",
            "apply_targets": [".agents/skills"],
            "apply_mode": "symlink",
            "prompts": {"ask_apply": False},
            "custom_agents": {},
        },
    )
    monkeypatch.chdir(env["proj"])

    presets_mod.apply("mypreset")

    applied = env["proj"] / ".agents" / "skills" / "tdd"
    assert applied.is_dir() and not applied.is_symlink(), "should have copied"
    assert (applied / "SKILL.md").exists()


def test_loader_files_are_packaged():
    """runner.py loads these at runtime; pyproject must ship them in the wheel."""
    assert "assets/*.mjs" in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for name in ("register.mjs", "loader.mjs"):
        assert (REPO_ROOT / "skill_ctl" / "assets" / name).exists()


def test_update_refuses_a_preset_without_a_lockfile(env):
    """Without skills-lock.json, `npx skills update` silently updates global skills
    instead of the preset, so skctl must refuse before handing off."""
    res = skctl(env, "update", "--preset", "mypreset")
    assert res.returncode == 1
    assert "skills-lock.json" in res.stdout + res.stderr


def test_update_accepts_a_preset_with_a_lockfile(env, monkeypatch):
    """With a lockfile present the guard stays out of the way and npx is invoked."""
    (env["presets"] / "mypreset" / "skills-lock.json").write_text(
        json.dumps({"version": 1, "skills": {}}), encoding="utf-8"
    )
    calls = []
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(skills_mod, "PRESETS_DIR", env["presets"])
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(
        skills_mod,
        "run_npx_skills",
        lambda args, cwd: calls.append((args, cwd)) or 0,
    )

    skills_mod.update(preset="mypreset")

    assert len(calls) == 1, "npx skills should have been invoked once"
    args, cwd = calls[0]
    assert args[0] == "update"
    assert cwd == str(env["presets"] / "mypreset")


def test_update_all_updates_locked_presets_and_skips_legacy_ones(env, monkeypatch):
    """`--all` never lets a legacy preset fall back to a global update."""
    locked = env["presets"] / "mypreset"
    (locked / "skills-lock.json").write_text(
        json.dumps({"version": 1, "skills": {}}), encoding="utf-8"
    )
    (env["presets"] / "legacy").mkdir()
    calls = []
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(skills_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(
        skills_mod,
        "run_npx_skills",
        lambda args, cwd: calls.append((args, cwd)) or 0,
    )

    skills_mod.update(all_presets=True)

    assert calls == [(["update"], str(locked))]


def test_add_installed_to_global(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(Path, "home", lambda: env["home"])

    config = {
        "apply_targets": [".agents/skills"],
        "apply_mode": "symlink",
        "custom_agents": {},
    }

    count = presets_mod.add_installed_to_global(config, skill="tdd")
    assert count == 1
    global_tdd = env["home"] / ".agents" / "skills" / "tdd"
    assert global_tdd.is_symlink()
    assert (global_tdd / "SKILL.md").exists()


def test_add_installed_to_global_copy(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(Path, "home", lambda: env["home"])

    config = {
        "apply_targets": [".agents/skills"],
        "apply_mode": "symlink",
        "custom_agents": {},
    }

    count = presets_mod.add_installed_to_global(config, skill="review", use_copy=True)
    assert count == 1
    global_review = env["home"] / ".agents" / "skills" / "review"
    assert global_review.is_dir() and not global_review.is_symlink()
    assert (global_review / "SKILL.md").exists()


def test_add_without_package_to_project_searches_remote(env, monkeypatch):
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(skills_mod.sys.stdin, "isatty", lambda: True)
    seen = {}

    def fake_search(**kwargs):
        seen["search"] = kwargs

    monkeypatch.setattr("skill_ctl.search.search", fake_search)
    skills_mod.add(package=None, project=str(env["proj"]))
    assert seen["search"]["project"] == str(env["proj"])
    assert seen["search"]["remote_only"] is True


def test_add_without_package_to_global_searches_remote(env, monkeypatch):
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(skills_mod.sys.stdin, "isatty", lambda: True)
    seen = {}

    def fake_search(**kwargs):
        seen["search"] = kwargs

    monkeypatch.setattr("skill_ctl.search.search", fake_search)
    skills_mod.add(package=None, global_install=True)
    assert seen["search"]["global_only"] is True
    assert seen["search"]["remote_only"] is True


def test_add_interactive_destination_prompt_project(env, monkeypatch):
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(skills_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        skills_mod,
        "load_config",
        lambda: {
            "default_destination": "prompt",
            "apply_targets": [".agents/skills"],
            "apply_mode": "symlink",
            "prompts": {"ask_destination": True, "picker": "none"},
            "custom_agents": {},
        },
    )
    monkeypatch.setattr(skills_mod, "prompt_destination", lambda *a, **k: (None, True, False))
    seen = {}

    def fake_search(**kwargs):
        seen["search"] = kwargs

    monkeypatch.setattr("skill_ctl.search.search", fake_search)
    skills_mod.add(package=None)
    assert seen["search"]["project"] == "."
    assert seen["search"]["remote_only"] is True


def test_add_interactive_destination_prompt_global(env, monkeypatch):
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(skills_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        skills_mod,
        "load_config",
        lambda: {
            "default_destination": "prompt",
            "apply_targets": [".agents/skills"],
            "apply_mode": "symlink",
            "prompts": {"ask_destination": True, "picker": "none"},
            "custom_agents": {},
        },
    )
    monkeypatch.setattr(skills_mod, "prompt_destination", lambda *a, **k: (None, False, True))
    seen = {}

    def fake_search(**kwargs):
        seen["search"] = kwargs

    monkeypatch.setattr("skill_ctl.search.search", fake_search)
    skills_mod.add(package=None)
    assert seen["search"]["global_only"] is True
    assert seen["search"]["remote_only"] is True


def test_apply_global_flag(env):
    res = skctl(env, "apply", "mypreset", "-g")
    assert res.returncode == 0, res.stderr
    global_tdd = env["home"] / ".agents" / "skills" / "tdd"
    global_review = env["home"] / ".agents" / "skills" / "review"
    assert global_tdd.is_symlink()
    assert global_review.is_symlink()
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()


def test_unapply_global_flag(env):
    res = skctl(env, "apply", "mypreset", "-g")
    assert res.returncode == 0, res.stderr
    global_tdd = env["home"] / ".agents" / "skills" / "tdd"
    assert global_tdd.is_symlink()

    res2 = skctl(env, "unapply", "mypreset", "-g", "-y")
    assert res2.returncode == 0, res2.stderr
    assert not global_tdd.exists()


def test_presets_edit_remove_flag(env):
    assert (env["presets"] / "mypreset" / ".agents" / "skills" / "review").is_dir()
    res = skctl(env, "presets", "edit", "mypreset", "--remove", "review")
    assert res.returncode == 0, res.stderr
    assert not (env["presets"] / "mypreset" / ".agents" / "skills" / "review").exists()
    assert (env["presets"] / "mypreset" / ".agents" / "skills" / "tdd").is_dir()


def test_presets_edit_add_flag(env):
    assert not (env["presets"] / "otherp" / ".agents" / "skills" / "review").exists()
    res = skctl(env, "presets", "edit", "otherp", "--add", "review")
    assert res.returncode == 0, res.stderr
    assert (env["presets"] / "otherp" / ".agents" / "skills" / "review").is_dir()


def test_presets_edit_interactive_menu_done(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod.Prompt, "ask", lambda *a, **k: "5")
    presets_mod.edit_preset("mypreset")
    assert (env["presets"] / "mypreset" / ".agents" / "skills" / "tdd").is_dir()


def test_apply_interactive_destination_prompt_global(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod, "load_config", lambda: {})
    monkeypatch.setattr(Path, "home", lambda: env["home"])
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod, "prompt_apply_destination", lambda target: "2")
    monkeypatch.setattr(presets_mod, "prompt_apply_type", lambda: "2")
    monkeypatch.setattr(presets_mod, "prompt_preset", lambda *a, **k: "mypreset")
    monkeypatch.setattr(presets_mod.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.chdir(env["proj"])

    presets_mod.apply()

    assert (env["home"] / ".agents" / "skills" / "tdd").is_symlink()
    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()


def test_apply_confirmation_declined(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod, "prompt_apply_destination", lambda *a, **k: "1")
    monkeypatch.setattr(presets_mod.Confirm, "ask", lambda *a, **k: False)
    monkeypatch.chdir(env["proj"])

    presets_mod.apply(preset="mypreset")

    assert not (env["proj"] / ".agents" / "skills" / "tdd").exists()


def test_unapply_confirmation_declined(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.chdir(env["proj"])

    # Apply first non-interactively
    presets_mod.apply(preset="mypreset", yes=True)
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()

    # Try unapply with confirmation declined
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod, "prompt_apply_destination", lambda *a, **k: "1")
    monkeypatch.setattr(presets_mod.Confirm, "ask", lambda *a, **k: False)

    presets_mod.unapply(preset="mypreset")
    assert (env["proj"] / ".agents" / "skills" / "tdd").is_symlink()


def test_unapply_global_multiple_presets(env):
    make_skill(env["presets"] / "otherp" / ".agents" / "skills", "other_skill")
    res1 = skctl(env, "apply", "mypreset", "-g", "-y")
    assert res1.returncode == 0, res1.stderr
    res2 = skctl(env, "apply", "otherp", "-g", "-y")
    assert res2.returncode == 0, res2.stderr

    global_review = env["home"] / ".agents" / "skills" / "review"
    global_other = env["home"] / ".agents" / "skills" / "other_skill"
    assert global_review.is_symlink()
    assert global_other.is_symlink()

    # Unapply only mypreset globally
    res3 = skctl(env, "unapply", "mypreset", "-g", "-y")
    assert res3.returncode == 0, res3.stderr
    assert not global_review.exists()
    assert global_other.is_symlink()


def test_unapply_global_with_skill_filter(env):
    res1 = skctl(env, "apply", "mypreset", "-g", "-y")
    assert res1.returncode == 0, res1.stderr

    global_tdd = env["home"] / ".agents" / "skills" / "tdd"
    global_review = env["home"] / ".agents" / "skills" / "review"
    assert global_tdd.is_symlink()
    assert global_review.is_symlink()

    # Unapply only review
    res2 = skctl(env, "unapply", "mypreset", "-s", "review", "-g", "-y")
    assert res2.returncode == 0, res2.stderr
    assert not global_review.exists()
    assert global_tdd.is_symlink()


def test_unapply_interactive_destination_prompt_global(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(Path, "home", lambda: env["home"])
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        presets_mod,
        "load_config",
        lambda: {
            "default_preset": "default",
            "apply_targets": [".agents/skills"],
            "apply_mode": "symlink",
            "prompts": {"ask_apply": True, "picker": "none"},
            "custom_agents": {},
        },
    )

    # Apply to global first
    presets_mod.apply(preset="mypreset", global_apply=True, yes=True)
    assert (env["home"] / ".agents" / "skills" / "tdd").is_symlink()

    # Unapply with destination prompt choosing "2" (Global)
    monkeypatch.setattr(presets_mod, "prompt_apply_destination", lambda *a, **k: "2")
    monkeypatch.setattr(presets_mod.Confirm, "ask", lambda *a, **k: True)

    presets_mod.unapply(preset="mypreset")
    assert not (env["home"] / ".agents" / "skills" / "tdd").exists()


def test_presets_browse_enter_opens_search(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod, "browse_presets", lambda dirs: (None, ["mypreset"]))

    search_called = {}

    def fake_search(**kwargs):
        search_called.update(kwargs)

    import skill_ctl.search

    monkeypatch.setattr(skill_ctl.search, "search", fake_search)

    presets_mod.presets(action="browse")
    assert search_called.get("preset") == "mypreset"


def test_presets_browse_enter_multiple_opens_search(env, monkeypatch):
    from skill_ctl import presets as presets_mod

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(presets_mod, "browse_presets", lambda dirs: (None, ["mypreset", "otherp"]))

    search_called = {}

    def fake_search(**kwargs):
        search_called.update(kwargs)

    import skill_ctl.search

    monkeypatch.setattr(skill_ctl.search, "search", fake_search)

    presets_mod.presets(action="browse")
    assert search_called.get("preset") == "mypreset,otherp"


def test_update_all_concurrency(env, monkeypatch):
    from skill_ctl import skills as skills_mod

    monkeypatch.setattr(skills_mod, "PRESETS_DIR", env["presets"])

    (env["presets"] / "mypreset" / "skills-lock.json").write_text("{}", encoding="utf-8")
    (env["presets"] / "otherp" / "skills-lock.json").write_text("{}", encoding="utf-8")

    calls = []

    def fake_run(args, cwd):
        calls.append((args, cwd))
        return 0

    monkeypatch.setattr(skills_mod, "run_npx_skills", fake_run)
    monkeypatch.setattr(skills_mod, "maybe_auto_push", lambda reason: None)

    skills_mod.update(all_presets=True, yes=True, concurrency=2)

    assert len(calls) == 2
    cwds = {c[1] for c in calls}
    assert cwds == {str(env["presets"] / "mypreset"), str(env["presets"] / "otherp")}
