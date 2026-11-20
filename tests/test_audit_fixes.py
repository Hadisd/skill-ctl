"""Regression tests for bugs identified in the repository audit."""

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _make_skill(root: Path, name: str, text: str = "# skill\n") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


def test_apply_and_unapply_succeed_under_symlinked_project_root(tmp_path, monkeypatch):
    from skill_ctl import presets as presets_mod
    from skill_ctl.core import registry
    from skill_ctl.core.linker import apply_skills

    presets_dir = tmp_path / "presets"
    src = _make_skill(presets_dir / "alpha" / ".agents" / "skills", "demo")
    real_proj = tmp_path / "real_proj"
    real_proj.mkdir()
    link_proj = tmp_path / "link_proj"
    try:
        link_proj.symlink_to(real_proj, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")

    res = apply_skills(link_proj, {"demo": src}, [".agents/skills"], copy=False)
    assert res.applied == {"demo": src}
    assert (link_proj / ".agents" / "skills" / "demo").is_symlink()

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(registry, "APPLIED_FILE", tmp_path / "applied.json")
    monkeypatch.setattr(
        presets_mod,
        "load_config",
        lambda: {"apply_targets": [".agents/skills"], "apply_mode": "symlink", "custom_agents": {}},
    )

    presets_mod.apply(preset="alpha", project=str(link_proj), yes=True, force=True)
    assert (real_proj / ".agents" / "skills" / "demo").is_symlink()

    # Also exercise unapply_one with unresolved link_proj directly, and global apply/unapply under symlinked HOME
    presets_mod.unapply_one(
        "alpha",
        link_proj,
        agent=None,
        force=False,
        config={"apply_targets": [".agents/skills"], "custom_agents": {}},
    )
    assert not (real_proj / ".agents" / "skills" / "demo").exists()

    real_home = tmp_path / "real_home"
    real_home.mkdir()
    link_home = tmp_path / "link_home"
    link_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: link_home)
    presets_mod.apply(preset="alpha", global_apply=True, yes=True)
    assert (real_home / ".agents" / "skills" / "demo").is_symlink()
    presets_mod.unapply(preset="alpha", global_unapply=True, yes=True)
    assert not (real_home / ".agents" / "skills" / "demo").exists()


def test_unapply_does_not_steal_unrelated_or_other_preset_skills(tmp_path, monkeypatch):
    from skill_ctl import presets as presets_mod
    from skill_ctl.core import registry

    presets_dir = tmp_path / "presets"
    _make_skill(presets_dir / "A" / ".agents" / "skills", "foo", "# from A\n")
    _make_skill(presets_dir / "B" / ".agents" / "skills", "foo", "# from B\n")
    proj = tmp_path / "proj"
    proj.mkdir()

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(registry, "APPLIED_FILE", tmp_path / "applied.json")
    monkeypatch.setattr(
        presets_mod,
        "load_config",
        lambda: {
            "apply_targets": [".agents/skills", ".claude/skills"],
            "apply_mode": "symlink",
            "custom_agents": {},
        },
    )

    presets_mod.apply(preset="A", agent="universal", project=str(proj), yes=True)
    presets_mod.apply(preset="B", agent="claude", project=str(proj), yes=True)

    # Plain unapply of A must remove A's link only and drop A from records.
    presets_mod.unapply(preset="A", project=str(proj), yes=True)
    assert not (proj / ".agents" / "skills" / "foo").exists()
    assert (proj / ".claude" / "skills" / "foo").is_symlink()
    rec = registry.read_project_file(proj)
    assert "A" not in rec
    assert "B" in rec

    # Re-apply A as a copy in .agents/skills, then run unapply A --force:
    # B's symlink and an unowned standalone skill in .cursor/skills must survive.
    presets_mod.apply(preset="A", agent="universal", copy=True, project=str(proj), yes=True)
    standalone = _make_skill(proj / ".cursor" / "skills", "foo", "# user written\n")
    presets_mod.unapply(preset="A", project=str(proj), force=True, yes=True)
    assert not (proj / ".agents" / "skills" / "foo").exists()
    assert (proj / ".claude" / "skills" / "foo").is_symlink()
    assert (standalone / "SKILL.md").read_text(encoding="utf-8") == "# user written\n"


def test_local_search_with_preset_filter_applies_to_project(tmp_path, monkeypatch):
    from skill_ctl import presets as presets_mod
    from skill_ctl import search as search_mod
    from skill_ctl.core import registry

    presets_dir = tmp_path / "presets"
    _make_skill(presets_dir / "p1" / ".agents" / "skills", "s1")
    _make_skill(presets_dir / "p2" / ".agents" / "skills", "s2")
    proj = tmp_path / "proj"
    proj.mkdir()

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(search_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(registry, "APPLIED_FILE", tmp_path / "applied.json")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    picked = {
        "skill": "s1",
        "preset": "p1",
        "scope": "preset",
        "path": str(presets_dir / "p1" / ".agents" / "skills" / "s1"),
        "name": "s1",
        "description": "",
        "location": "p1",
    }
    monkeypatch.setattr(search_mod, "pick", lambda *a, **k: [picked])
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **k: True)

    search_mod.search(preset="p1,p2", project=str(proj))
    assert not (presets_dir / "p1,p2").exists()
    assert (proj / ".agents" / "skills" / "s1").is_symlink()


def test_offer_add_and_edit_preset_pass_remote_only(tmp_path, monkeypatch):
    from skill_ctl import presets as presets_mod

    presets_dir = tmp_path / "presets"
    preset_dir = presets_dir / "demo"
    preset_dir.mkdir(parents=True)
    monkeypatch.setattr(presets_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)

    calls = []
    monkeypatch.setattr("skill_ctl.search.search", lambda **kw: calls.append(kw))
    monkeypatch.setattr(presets_mod.Prompt, "ask", lambda *a, **k: "2")
    presets_mod.offer_add_after_create("demo", preset_dir, {"prompts": {}})
    assert calls == [{"preset": "demo", "remote_only": True}]

    calls.clear()
    answers = iter(["2", "5"])
    monkeypatch.setattr(presets_mod.Prompt, "ask", lambda *a, **k: next(answers))
    presets_mod.edit_preset("demo", config={"prompts": {}})
    assert calls == [{"preset": "demo", "remote_only": True}]


def test_ensure_config_does_not_append_to_invalid_yaml(tmp_path, monkeypatch):
    from skill_ctl.core import config as cfg

    cfg_file = tmp_path / "config.yaml"
    broken = "theme: [unclosed\n"
    cfg_file.write_text(broken, encoding="utf-8")

    monkeypatch.setattr(cfg, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", cfg_file)
    cfg.invalidate_config_cache()

    with pytest.warns(RuntimeWarning, match="Could not parse"):
        cfg.ensure_config()
    cfg.invalidate_config_cache()
    with pytest.warns(RuntimeWarning, match="Could not parse"):
        cfg.ensure_config()
    assert cfg_file.read_text(encoding="utf-8") == broken


def test_rename_preset_directory_restores_first_link_even_if_backup_replace_fails(
    tmp_path, monkeypatch
):
    from skill_ctl.core.linker import rename_preset_directory

    old_dir = tmp_path / "presets" / "old"
    new_dir = tmp_path / "presets" / "new"
    src = _make_skill(old_dir / ".agents" / "skills", "a")
    proj = tmp_path / "proj"
    for rel in (".agents/skills", ".claude/skills"):
        (proj / rel).mkdir(parents=True)
        (proj / rel / "a").symlink_to(src, target_is_directory=True)

    orig_replace = Path.replace

    def fail_claude(self, target):
        if ".claude" in str(target):
            raise PermissionError("read-only")
        return orig_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_claude)
    with pytest.raises(PermissionError):
        rename_preset_directory(old_dir, new_dir, {proj: [".agents/skills", ".claude/skills"]})

    assert old_dir.is_dir()
    assert not new_dir.exists()
    assert (proj / ".agents" / "skills" / "a").resolve() == src.resolve()


def test_doctor_fix_keeps_mixed_copy_record_and_preserves_newly_indexed_clone(
    tmp_path, monkeypatch
):
    import shutil

    from skill_ctl import presets as presets_mod
    from skill_ctl.commands import doctor as doctor_mod
    from skill_ctl.core import registry

    presets_dir = tmp_path / "presets"
    _make_skill(presets_dir / "A" / ".agents" / "skills", "s1")
    _make_skill(presets_dir / "A" / ".agents" / "skills", "s2")
    _make_skill(presets_dir / "B" / ".agents" / "skills", "live")
    proj_mixed = tmp_path / "proj_mixed"
    proj_clone = tmp_path / "proj_clone"
    proj_mixed.mkdir()
    proj_clone.mkdir()

    applied_file = tmp_path / "applied.json"
    monkeypatch.setattr(presets_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(doctor_mod, "preset_path", lambda n: presets_dir / n)
    monkeypatch.setattr(registry, "APPLIED_FILE", applied_file)
    cfg = {"apply_targets": [".agents/skills"], "apply_mode": "symlink", "custom_agents": {}}
    monkeypatch.setattr(presets_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(doctor_mod, "load_config", lambda: cfg)

    presets_mod.apply(preset="A", skill="s1", copy=True, project=str(proj_mixed), yes=True)
    presets_mod.apply(preset="A", skill="s2", copy=False, project=str(proj_mixed), yes=True)
    shutil.rmtree(presets_dir / "A")

    doctor_mod.doctor(project=str(proj_mixed), fix=True)
    assert not (proj_mixed / ".agents" / "skills" / "s2").exists()
    assert "A" in registry.read_project_file(proj_mixed)

    presets_mod.apply(preset="B", project=str(proj_clone), yes=True)
    # Remove proj_clone from global applied.json to simulate a fresh clone, then run doctor --fix.
    idx = registry.load_applied()
    idx.pop(str(proj_clone), None)
    registry.save_applied(idx)

    doctor_mod.doctor(project=str(proj_clone), fix=True)
    assert str(proj_clone) in registry.load_applied()


def test_apply_all_presets_forwards_skill_and_skips_prompt_preset(tmp_path, monkeypatch):
    from skill_ctl import presets as presets_mod
    from skill_ctl.core import registry

    presets_dir = tmp_path / "presets"
    _make_skill(presets_dir / "p1" / ".agents" / "skills", "keep")
    _make_skill(presets_dir / "p2" / ".agents" / "skills", "other")
    proj = tmp_path / "proj"
    proj.mkdir()

    monkeypatch.setattr(presets_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(registry, "APPLIED_FILE", tmp_path / "applied.json")
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod, "prompt_apply_destination", lambda *a, **k: "1")
    monkeypatch.setattr(
        presets_mod,
        "prompt_preset",
        lambda *a, **k: pytest.fail("prompt_preset should not be called with --all-presets"),
    )
    monkeypatch.setattr(
        presets_mod,
        "load_config",
        lambda: {
            "apply_targets": [".agents/skills"],
            "apply_mode": "symlink",
            "prompts": {"ask_apply": True},
            "custom_agents": {},
        },
    )

    presets_mod.apply(all_presets=True, skill="keep", project=str(proj), yes=True)
    assert (proj / ".agents" / "skills" / "keep").is_symlink()
    assert not (proj / ".agents" / "skills" / "other").exists()


def test_missing_preset_dir_does_not_crash_scan_picker_or_diff(tmp_path, monkeypatch):
    from skill_ctl.commands import diff as diff_mod
    from skill_ctl.core import registry
    from skill_ctl.core.skillscan import get_preset_skills
    from skill_ctl.ui import prompts

    assert get_preset_skills(tmp_path / "does-not-exist") == {}

    presets_dir = tmp_path / "presets"
    for i in range(12):
        (presets_dir / f"p{i}").mkdir(parents=True)
    names = sorted(d.name for d in presets_dir.iterdir())
    monkeypatch.setattr(prompts, "_use_preset_picker", lambda count, cfg: True)
    monkeypatch.setattr(prompts, "load_config", lambda: {})
    monkeypatch.setattr(
        prompts.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="2\tp0\t/tmp/x\n"),
    )
    assert (
        prompts.prompt_preset_or_new(names, default_preset="default", presets_dir=presets_dir)
        == "p0"
    )

    live_dir = presets_dir / "live"
    _make_skill(live_dir / ".agents" / "skills", "s1")
    proj = tmp_path / "proj"
    _make_skill(proj / ".agents" / "skills", "s1")
    (proj / "skills-applied.json").write_text(
        json.dumps(
            {
                "version": 1,
                "presets": {
                    "gone": {"skills": ["s0"], "targets": [".agents/skills"], "mode": "symlink"},
                    "live": {"skills": ["s1"], "targets": [".agents/skills"], "mode": "symlink"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(diff_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(registry, "APPLIED_FILE", tmp_path / "applied.json")
    diff_mod.diff_cmd(project=str(proj), skill="s1", json_output=True)


def test_sanitize_url_and_backup_output_hide_empty_user_tokens(tmp_path, monkeypatch, capsys):
    from skill_ctl.commands import backup as backup_mod

    secret = "ghp_super_secret_token"
    raw_url = f"https://:{secret}@github.com/org/repo.git"
    assert backup_mod.sanitize_url(raw_url) == "https://***@github.com/org/repo.git"
    assert (
        backup_mod.sanitize_url("https://user:p@ss@github.com/org/repo.git")
        == "https://***@github.com/org/repo.git"
    )
    assert (
        backup_mod.sanitize_url("https://github.com/org/@scoped/repo.git")
        == "https://github.com/org/@scoped/repo.git"
    )

    monkeypatch.setattr(backup_mod, "BASE_DIR", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(backup_mod, "remote_url", lambda: raw_url)
    monkeypatch.setattr(
        backup_mod,
        "git",
        lambda args, capture=False: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    backup_mod.backup_status()
    assert secret not in capsys.readouterr().out

    monkeypatch.setattr(backup_mod, "ensure_local_repo", lambda: None)
    monkeypatch.setattr(backup_mod, "create_github_repo", lambda name, priv: None)
    with pytest.raises(SystemExit):
        backup_mod.backup_init("newrepo", False, yes=True)
    out = capsys.readouterr()
    assert secret not in out.out + out.err


def test_archive_extract_counts_actual_bytes_and_export_handles_internal_symlinks(
    tmp_path, monkeypatch
):
    import io

    from skill_ctl.core import archive as archive_mod

    zip_path = tmp_path / "spoofed.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"format": 1, "preset": "evil"}))
        zf.writestr("preset/big.txt", b"A" * 50)

    orig_infolist = zipfile.ZipFile.infolist
    orig_open = zipfile.ZipFile.open

    def spoofed_infolist(self):
        infos = orig_infolist(self)
        for info in infos:
            info.file_size = 1
        return infos

    def oversized_open(self, name, *args, **kwargs):
        target_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if target_name == "preset/big.txt":
            return io.BytesIO(b"A" * 500)
        return orig_open(self, name, *args, **kwargs)

    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_BYTES", 100)
    monkeypatch.setattr(zipfile.ZipFile, "infolist", spoofed_infolist)
    monkeypatch.setattr(zipfile.ZipFile, "open", oversized_open)
    with pytest.raises(ValueError, match="expands beyond"):
        archive_mod.safe_extract_preset(zip_path, tmp_path / "dest")

    preset_dir = tmp_path / "preset_src"
    skill_dir = _make_skill(preset_dir / ".agents" / "skills", "demo")
    (preset_dir / ".claude" / "skills").mkdir(parents=True)
    try:
        (preset_dir / ".claude" / "skills" / "demo").symlink_to(
            "../../.agents/skills/demo", target_is_directory=True
        )
        (skill_dir / "loop").symlink_to("loop")
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")

    exported = tmp_path / "exported.skctl-preset.zip"
    archive_mod.export_preset(preset_dir, "preset_src", exported, ["demo"])
    with zipfile.ZipFile(exported) as zf:
        names = set(zf.namelist())
        assert "preset/.agents/skills/demo/SKILL.md" in names
        assert "preset/.claude/skills/demo/" not in names


def test_no_color_does_not_overwrite_saved_theme_in_config(tmp_path, monkeypatch):
    from skill_ctl.commands import config_commands as cc
    from skill_ctl.core import config as cfg
    from skill_ctl.ui import prompts

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("theme: default\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", cfg_file)
    cfg.invalidate_config_cache()
    monkeypatch.setenv("NO_COLOR", "1")

    cc.theme_cmd(name="tokyo-night")
    assert "theme: tokyo-night" in cfg_file.read_text(encoding="utf-8")

    monkeypatch.setattr(prompts, "_ask", lambda *a, **k: "tokyo")
    assert prompts.prompt_theme(["default", "tokyo-night"], "default") == "tokyo-night"


def test_preset_import_github_and_atomic_replace_safeguards(tmp_path, monkeypatch):
    from skill_ctl.commands import preset_remote, presets

    presets_dir = tmp_path / "presets"
    existing = presets_dir / "keepme"
    _make_skill(existing / ".agents" / "skills", "orig", "# keep\n")
    other = presets_dir / "src"
    _make_skill(other / ".agents" / "skills", "s2")

    monkeypatch.setattr(preset_remote, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(presets, "PRESETS_DIR", presets_dir)

    # Atomic replace preserves existing preset if copytree fails
    orig_copytree = presets.shutil.copytree

    def failing_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(presets.shutil, "copytree", failing_copy)
    with pytest.raises(OSError, match="disk full"):
        presets.clone_preset("src", "keepme", replace=True)
    assert (existing / ".agents" / "skills" / "orig" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# keep\n"
    monkeypatch.setattr(presets.shutil, "copytree", orig_copytree)

    # Multi-select + --rename rejected and existing preset without --replace exits 1
    repo_root = tmp_path / "remote_repo"
    _make_skill(repo_root / "presets" / "p1" / ".agents" / "skills", "a")
    _make_skill(repo_root / "presets" / "p2" / ".agents" / "skills", "b")

    def fake_git_clone(cmd, **kwargs):
        dest = Path(cmd[-1])
        orig_copytree(repo_root, dest)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(preset_remote.shutil, "which", lambda cmd: "/usr/bin/git")
    monkeypatch.setattr(preset_remote.subprocess, "run", fake_git_clone)
    monkeypatch.setattr(preset_remote, "pick_remote_presets", lambda avail: ["p1", "p2"])

    with pytest.raises(SystemExit) as exc1:
        preset_remote.import_github_presets("owner/repo", [], rename="single")
    assert exc1.value.code == 1

    # Single-preset conflict without --replace exits 1; multi-preset import calls maybe_auto_push once
    pushed = []
    monkeypatch.setattr(preset_remote, "maybe_auto_push", lambda msg: pushed.append(msg))
    preset_remote.import_github_presets("owner/repo", ["p1", "p2"])
    assert len(pushed) == 1

    with pytest.raises(SystemExit) as exc2:
        preset_remote.import_github_presets("owner/repo", ["p1"], replace=False)
    assert exc2.value.code == 1


def test_restore_global_skills_updates_symlinked_dir_and_replaces_file_collision(
    tmp_path, monkeypatch
):
    from skill_ctl.commands import backup as backup_mod

    home = tmp_path / "home"
    base_dir = home / ".skill-ctl"
    monkeypatch.setattr(backup_mod, "BASE_DIR", base_dir)
    monkeypatch.setattr(Path, "home", lambda: home)

    agents_dir = home / ".agents" / "skills"
    agents_dir.mkdir(parents=True)
    claude_dir = home / ".claude" / "skills"
    claude_dir.parent.mkdir(parents=True)
    try:
        claude_dir.symlink_to(agents_dir, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")

    # Old copy inside symlinked claude/skills + colliding regular file in .agents/skills
    _make_skill(claude_dir, "custom", "# old\n")
    (agents_dir / "file_collision").write_text("not a dir", encoding="utf-8")

    _make_skill(base_dir / "global" / ".claude" / "skills", "custom", "# updated\n")
    _make_skill(base_dir / "global" / ".agents" / "skills", "file_collision", "# replaced\n")

    cfg = {"global_skill_dirs": ["~/.agents/skills", "~/.claude/skills"]}
    backup_mod.restore_global_skills(config=cfg)

    assert (claude_dir / "custom" / "SKILL.md").read_text(encoding="utf-8") == "# updated\n"
    assert (agents_dir / "file_collision" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# replaced\n"


def test_search_global_only_and_alt_x_cleanup(tmp_path, monkeypatch):
    from skill_ctl import presets as presets_mod
    from skill_ctl import search as search_mod
    from skill_ctl.core import registry

    home = tmp_path / "home"
    presets_dir = home / ".skill-ctl" / "presets"
    _make_skill(presets_dir / "p1" / ".agents" / "skills", "alpha")
    (presets_dir / "p1" / ".claude" / "skills").mkdir(parents=True)
    try:
        (presets_dir / "p1" / ".claude" / "skills" / "alpha").symlink_to(
            presets_dir / "p1" / ".agents" / "skills" / "alpha", target_is_directory=True
        )
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")
    (presets_dir / "p1" / "skills-lock.json").write_text(
        json.dumps({"version": 1, "skills": {"alpha": {"source": "o/r"}}}), encoding="utf-8"
    )
    _make_skill(presets_dir / "p2" / ".agents" / "skills", "beta")
    standalone = _make_skill(home / ".agents" / "skills", "solo")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(presets_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(search_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(registry, "APPLIED_FILE", home / ".skill-ctl" / "applied.json")
    monkeypatch.setattr(search_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        search_mod,
        "load_config",
        lambda: {"global_skill_dirs": [str(home / ".agents" / "skills")]},
    )

    # Plain Enter in search --global must not pass standalone skills to add_installed_to_global
    monkeypatch.setattr(
        search_mod,
        "add_installed_to_global",
        lambda *a, **k: pytest.fail("add_installed_to_global should not be called"),
    )
    monkeypatch.setattr(
        search_mod,
        "pick",
        lambda rows, **k: [
            {
                "skill": "solo",
                "preset": "~/.agents/skills",
                "scope": "global",
                "path": str(standalone),
            }
        ],
    )
    search_mod.search(global_only=True)

    # Alt-X across two presets removes sibling links, lockfile entry, and calls maybe_auto_push once
    pushed = []
    monkeypatch.setattr(search_mod, "maybe_auto_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **k: True)
    monkeypatch.setattr(
        search_mod,
        "pick",
        lambda rows, **k: [
            {
                "skill": "alpha",
                "preset": "p1",
                "scope": "preset",
                "path": str(presets_dir / "p1" / ".agents" / "skills" / "alpha"),
                "_action": "alt-x",
            },
            {
                "skill": "beta",
                "preset": "p2",
                "scope": "preset",
                "path": str(presets_dir / "p2" / ".agents" / "skills" / "beta"),
                "_action": "alt-x",
            },
        ],
    )
    search_mod.search(project=str(tmp_path / "proj"))
    assert not (presets_dir / "p1" / ".claude" / "skills" / "alpha").exists()
    assert not (presets_dir / "p1" / "skills-lock.json").exists()
    assert len(pushed) == 1


def test_cli_flags_update_concurrency_single_preset_and_rename_cache(tmp_path, monkeypatch):
    from skill_ctl.commands import presets as presets_mod
    from skill_ctl.commands import skills as skills_mod
    from skill_ctl.core import config as cfg
    from skill_ctl.core import registry

    presets_dir = tmp_path / "presets"
    _make_skill(presets_dir / "solo" / ".agents" / "skills", "s1")
    (presets_dir / "solo" / "skills-lock.json").write_text('{"version":1,"skills":{}}')
    _make_skill(presets_dir / "two" / ".agents" / "skills", "s2")
    (presets_dir / "two" / "skills-lock.json").write_text('{"version":1,"skills":{}}')

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("default_preset: solo\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", cfg_file)
    cfg.invalidate_config_cache()
    monkeypatch.setattr(presets_mod, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(presets_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(skills_mod, "PRESETS_DIR", presets_dir)
    monkeypatch.setattr(registry, "APPLIED_FILE", tmp_path / "applied.json")
    monkeypatch.setattr(skills_mod, "maybe_auto_push", lambda *a: None)
    monkeypatch.setattr(presets_mod, "maybe_auto_push", lambda *a: None)

    calls = []
    monkeypatch.setattr(
        skills_mod,
        "run_npx_skills",
        lambda args, cwd=None, capture=False, stream=True: (
            calls.append(args),
            (0, "appear to have been deleted upstream:\n  • gone-skill\n") if capture else 0,
        )[1],
    )
    import inspect

    assert "force" not in inspect.signature(skills_mod.add).parameters
    monkeypatch.setattr(skills_mod, "get_detected_global_agents", lambda: ["universal"])
    skills_mod.add("owner/repo", global_install=True, yes=True)
    assert calls[0].count("--agent") == 1

    calls.clear()
    monkeypatch.setattr(skills_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(skills_mod, "wants_picker", lambda c: False)
    monkeypatch.setattr(skills_mod, "prompt_skills", lambda names, action="remove": ["s1"])
    skills_mod.remove(skill=None, preset="solo", agent="claude-code", yes=True)
    assert "--agent" in calls[0] and "claude-code" in calls[0]

    # Concurrent update --all warns on upstream-deleted skills when non-interactive
    warnings_seen = []
    monkeypatch.setattr(skills_mod, "print_warn", lambda msg: warnings_seen.append(msg))
    skills_mod.update(all_presets=True, yes=True, concurrency=2)
    assert any("gone-skill" in w for w in warnings_seen)

    # Rename default preset invalidates config cache
    assert cfg.load_config()["default_preset"] == "solo"
    presets_mod.presets(action="rename", name="solo", new_name="renamed-solo")
    assert cfg.load_config()["default_preset"] == "renamed-solo"

    # Single non-default preset with --pick selects it without creating 'default'
    import shutil

    shutil.rmtree(presets_dir / "two")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(presets_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(presets_mod, "prompt_apply_destination", lambda *a: "1")
    monkeypatch.setattr(presets_mod, "choose_skills", lambda *a: ["s1"])
    presets_mod.apply(pick_skills=True, project=str(proj), yes=True)
    assert not (presets_dir / "default").exists()
    assert (proj / ".agents" / "skills" / "s1").is_symlink()


def test_picker_windows_quotes_bat_theme_and_disk_cache_prunes_throttled(tmp_path, monkeypatch):
    from skill_ctl.commands import catalog
    from skill_ctl.ui import picker

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(picker.os, "name", "nt")
    monkeypatch.setattr(picker.shutil, "which", lambda cmd: "bat.exe" if cmd == "bat" else None)
    monkeypatch.setattr(picker, "load_config", lambda: {"theme": "catppuccin"})
    cmd = picker.preview_markdown_command("{5}")
    assert '--theme="Catppuccin Mocha"' in cmd

    cache_root = tmp_path / "cache"
    query_dir = cache_root / "queries"
    query_dir.mkdir(parents=True)
    monkeypatch.setattr(catalog, "CACHE_DIR", cache_root)
    monkeypatch.setattr(catalog, "QUERY_CACHE_DIR", query_dir)

    expired = query_dir / "expired.json"
    expired.write_text("{}", encoding="utf-8")
    old_ts = 1.0
    os.utime(expired, (old_ts, old_ts))

    catalog._write_disk_cache(query_dir / "fresh.json", [{"name": "ok"}])
    assert not expired.exists()
    assert (cache_root / ".last_pruned").exists()

    # Second write within interval is throttled (marker prevents rescanning)
    expired2 = query_dir / "expired2.json"
    expired2.write_text("{}", encoding="utf-8")
    os.utime(expired2, (old_ts, old_ts))
    catalog._write_disk_cache(query_dir / "fresh2.json", [{"name": "ok2"}])
    assert expired2.exists()
