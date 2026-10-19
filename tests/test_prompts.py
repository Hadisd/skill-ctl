"""Tests for the numbered prompts: 0 means everything, and is the default."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from skill_ctl import picker as picker_mod  # noqa: E402
from skill_ctl import ui as ui_mod  # noqa: E402


def answer(monkeypatch, *replies):
    """Feed the prompts canned answers, one per Prompt.ask call."""
    queue = list(replies)
    monkeypatch.setattr(ui_mod.Prompt, "ask", lambda *a, **k: queue.pop(0))


def test_zero_picks_every_skill(monkeypatch):
    answer(monkeypatch, "0")
    assert ui_mod.prompt_skills(["review", "tdd"]) == ["review", "tdd"]


def test_zero_is_the_default_so_enter_still_means_all(monkeypatch, capsys):
    """Rich returns the default when the line is empty, which must stay 'all'."""
    seen = {}

    def fake_ask(prompt, **kwargs):
        seen["default"] = kwargs.get("default")
        return kwargs.get("default")

    monkeypatch.setattr(ui_mod.Prompt, "ask", fake_ask)
    assert ui_mod.prompt_skills(["review", "tdd"]) == ["review", "tdd"]
    assert seen["default"] == "0"

    out = capsys.readouterr().out
    assert "0) all skills" in out
    assert "(default)" in out


def test_zero_wins_over_anything_else_on_the_line(monkeypatch):
    answer(monkeypatch, "2 0")
    assert ui_mod.prompt_skills(["review", "tdd"]) == ["review", "tdd"]


def test_skill_numbering_is_unchanged_by_the_zero_entry(monkeypatch):
    answer(monkeypatch, "2")
    assert ui_mod.prompt_skills(["review", "tdd"]) == ["tdd"]


def test_a_line_of_typos_still_applies_nothing(monkeypatch):
    """0 or a blank line mean everything; a mistake must not."""
    answer(monkeypatch, "nope")
    with pytest.raises(SystemExit) as exit_info:
        ui_mod.prompt_skills(["review", "tdd"])
    assert exit_info.value.code == 1


def test_preset_prompt_leads_with_all_presets_as_the_default(monkeypatch, capsys):
    """Naming a preset first is the wrong question when you only know the skill."""
    seen = {}

    def fake_ask(prompt, **kwargs):
        seen["default"] = kwargs.get("default")
        return kwargs.get("default")

    monkeypatch.setattr(ui_mod.Prompt, "ask", fake_ask)
    assert ui_mod.prompt_preset(["frontend", "writing"]) == ui_mod.ALL_PRESETS
    assert seen["default"] == "0"
    assert "0) all presets" in capsys.readouterr().out


def test_preset_prompt_still_takes_a_number_or_a_name(monkeypatch):
    answer(monkeypatch, "2")
    assert ui_mod.prompt_preset(["frontend", "writing"]) == "writing"

    answer(monkeypatch, "frontend")
    assert ui_mod.prompt_preset(["frontend", "writing"]) == "frontend"


def test_destination_menu_combines_default_pick_and_create(monkeypatch, tmp_path, capsys):
    """`add` asks where to put a skill; searching every preset is not an answer to that."""
    presets = tmp_path / "presets"
    (presets / "frontend").mkdir(parents=True)

    seen = {}

    def fake_ask(prompt, **kwargs):
        if kwargs.get("choices"):
            seen["choices"] = kwargs["choices"]
        return kwargs.get("default")

    monkeypatch.setattr(ui_mod.Prompt, "ask", fake_ask)

    assert ui_mod.prompt_destination(presets, "default") == (None, True, False)
    assert seen["choices"] == ["1", "2", "3"]
    assert "Pick from all presets" not in capsys.readouterr().out


def test_preset_or_new_has_create_new_as_zero_on_top(monkeypatch, capsys):
    answer(monkeypatch, "0", "brand-new")
    assert ui_mod.prompt_preset_or_new(["frontend", "writing"], default_preset="default") == "brand-new"
    out = capsys.readouterr().out
    assert "0) Create new preset" in out
    assert "1) default" in out
    assert "*" in out


def test_preset_or_new_defaults_to_default_preset(monkeypatch):
    answer(monkeypatch, "1")
    assert ui_mod.prompt_preset_or_new(["frontend"], default_preset="default") == "default"


def test_preset_or_new_selects_existing_or_typed_name(monkeypatch):
    answer(monkeypatch, "2")
    assert ui_mod.prompt_preset_or_new(["frontend"], default_preset="default") == "frontend"

    answer(monkeypatch, "custom-name")
    assert ui_mod.prompt_preset_or_new(["frontend"], default_preset="default") == "custom-name"



def test_fzf_binds_ctrl_a_to_select_all(monkeypatch):
    """Narrow by typing, ctrl-a, Enter: the whole filtered set in three keystrokes."""
    seen = {}

    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(picker_mod.shutil, "which", lambda name: "/usr/bin/fzf")
    monkeypatch.setattr(
        picker_mod.subprocess, "run",
        lambda cmd, **kwargs: seen.update(cmd=cmd) or Result(),
    )

    picker_mod.pick_with_fzf([{
        "skill": "tdd", "preset": "p", "description": "d", "path": "/tmp/tdd",
    }])

    binds = seen["cmd"][seen["cmd"].index("--bind") + 1]
    assert "ctrl-a:select-all" in binds
    assert "ctrl-d:deselect-all" in binds
    # And the keys are named in the header rather than left to be discovered.
    header = seen["cmd"][seen["cmd"].index("--header") + 1]
    assert "^A" in header


def test_prompt_apply_type_defaults_to_specific_skills(monkeypatch):
    seen = {}

    def fake_ask(prompt, **kwargs):
        seen["default"] = kwargs.get("default")
        seen["choices"] = kwargs.get("choices")
        return kwargs.get("default")

    monkeypatch.setattr(ui_mod.Prompt, "ask", fake_ask)
    assert ui_mod.prompt_apply_type() == "1"
    assert seen["default"] == "1"
    assert seen["choices"] == ["1", "2"]


def test_prompt_preset_without_all_presets_excludes_zero(monkeypatch, capsys):
    answer(monkeypatch, "1")
    assert ui_mod.prompt_preset(["frontend", "writing"], include_all=False) == "frontend"
    out = capsys.readouterr().out
    assert "0) all presets" not in out
    assert "1) frontend" in out
    assert "2) writing" in out


def test_browse_preset_choices(monkeypatch, tmp_path):
    from skill_ctl.prompts import _browse_preset_choices
    import json

    presets_dir = tmp_path / "presets"
    p1 = presets_dir / "preset1" / ".agents" / "skills" / "my-skill"
    p1.mkdir(parents=True)
    (p1 / "SKILL.md").write_text("---\nname: my-skill\n---\nHello")

    registry = tmp_path / "applied.json"
    registry.write_text(json.dumps({"/some/proj": ["preset1"]}))
    monkeypatch.setattr("skill_ctl.registry.APPLIED_FILE", registry)
    monkeypatch.setattr("skill_ctl.prompts.shutil.which", lambda name: "/usr/bin/fzf")

    class FakeResult:
        returncode = 0
        stdout = "1\tpreset1\n"

    monkeypatch.setattr("skill_ctl.prompts.subprocess.run", lambda *a, **k: FakeResult())

    chosen = _browse_preset_choices(["preset1"], {}, presets_dir, extra=("Create new", "Help text"))
    assert chosen == 1

