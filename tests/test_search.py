"""Tests for `skctl search`, which looks through the presets you already have."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SKILLS = {
    "latex-paper": ("frontend", "Write and build LaTeX papers, with bibliography handling."),
    "web-design": ("frontend", "Design guidelines for web interfaces and components."),
    "drawio": ("diagrams", "Create and edit diagrams as drawio XML."),
    "plain": ("diagrams", None),  # no frontmatter at all
}


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    presets = home / ".skill-ctl" / "presets"
    for name, (preset, description) in SKILLS.items():
        d = presets / preset / ".agents" / "skills" / name
        d.mkdir(parents=True)
        body = f"# {name}\n"
        if description:
            body = f"---\nname: {name}\ndescription: {description}\n---\n\n" + body
        (d / "SKILL.md").write_text(body, encoding="utf-8")
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


def flat(res) -> str:
    """Console output on one line: rich wraps at the terminal width mid-sentence."""
    return " ".join(res.stdout.split())


def rows(env, *args) -> list:
    res = skctl(env, "search", *args, "--json")
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_search_matches_a_skill_name(env):
    names = [r["skill"] for r in rows(env, "latex")]
    assert names == ["latex-paper"]


def test_search_matches_the_description(env):
    """The point of searching: you remember what it does, not what it is called."""
    found = rows(env, "bibliography")
    assert [r["skill"] for r in found] == ["latex-paper"]
    assert found[0]["preset"] == "frontend"


def test_every_word_has_to_match(env):
    assert [r["skill"] for r in rows(env, "diagrams drawio")] == ["drawio"]
    assert rows(env, "diagrams latex") == []


def test_search_is_case_insensitive(env):
    assert [r["skill"] for r in rows(env, "LaTeX")] == ["latex-paper"]


def test_a_skill_without_frontmatter_is_still_findable(env):
    found = rows(env, "plain")
    assert [r["skill"] for r in found] == ["plain"]
    assert found[0]["description"] == ""


def test_no_query_lists_everything_as_json(env):
    """--json is a deliberate ask for the data, so it still gets everything."""
    assert sorted(r["skill"] for r in rows(env)) == sorted(SKILLS)


def test_search_requires_a_terminal_unless_json_is_requested(env):
    res = skctl(env, "search")
    assert res.returncode == 1
    assert "interactive" in res.stderr
    assert "--json" in res.stderr


def test_scoped_search_also_requires_a_terminal(env):
    res = skctl(env, "search", "--preset", "diagrams")
    assert res.returncode == 1
    assert "interactive" in res.stderr


def test_bare_search_on_an_empty_preset_says_so(env, tmp_path):
    (env["presets"] / "empty" / ".agents" / "skills").mkdir(parents=True)
    out = flat(skctl(env, "search", "--preset", "empty"))
    assert "No skills in preset 'empty' yet" in out
    assert "skctl search --remote" in out


def test_applied_query_and_json_still_list_with_no_search_word(env):
    """Each of these says what you want without a query; none should be swallowed
    by the "give me a word" note."""
    assert skctl(env, "apply", "diagrams", "-s", "drawio").returncode == 0

    res = skctl(env, "search", "--applied")
    assert res.returncode == 1
    assert "interactive" in res.stderr


def test_preset_narrows_the_search(env):
    assert sorted(r["skill"] for r in rows(env, "--preset", "diagrams")) == ["drawio", "plain"]


def test_search_includes_configured_global_folders(env):
    global_skill = env["home"] / ".agents" / "skills" / "global-only"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text(
        "---\nname: global-only\ndescription: Available from every project.\n---\n",
        encoding="utf-8",
    )

    found = rows(env, "global-only")
    assert len(found) == 1
    assert found[0]["scope"] == "global"
    assert found[0]["location"] == "G: agents"


def test_format_global_location():
    from skill_ctl.picker import format_global_location

    assert format_global_location("~/.agents/skills") == "G: agents"
    assert format_global_location("~/.claude/skills") == "G: claude"
    assert format_global_location("~/.cursor/skills") == "G: cursor"
    assert format_global_location("~/.codex/skills") == "G: codex"
    assert format_global_location("~/.codeium/windsurf/skills") == "G: windsurf"
    assert format_global_location("~/.hermes/skills") == "G: hermes"
    assert format_global_location("~/shared-skills") == "G: shared-skills"
    assert format_global_location("global:~/.agents/skills") == "G: agents"


def test_global_filter_excludes_presets_and_uses_custom_paths(env):
    custom = env["home"] / "shared-skills"
    skill = custom / "custom-global"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# custom-global\n", encoding="utf-8")
    config = env["home"] / ".skill-ctl" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'global_skill_dirs:\n  - "{custom.as_posix()}"\n', encoding="utf-8")

    found = rows(env, "--global")
    assert [row["skill"] for row in found] == ["custom-global"]
    assert all(row["scope"] == "global" for row in found)
    assert found[0]["location"] == "G: shared-skills"


def test_unknown_preset_is_an_error(env):
    res = skctl(env, "search", "--preset", "nope")
    assert res.returncode == 1
    assert "nope" in res.stdout + res.stderr


def test_search_marks_what_is_applied_here(env):
    assert skctl(env, "apply", "diagrams", "-s", "drawio").returncode == 0

    applied = {r["skill"]: r["applied"] for r in rows(env)}
    assert applied["drawio"] is True
    assert applied["plain"] is False

    only = [r["skill"] for r in rows(env, "--applied")]
    assert only == ["drawio"]


def test_name_matches_rank_above_description_matches(env):
    """Substring matching is blunt, so the one you probably meant has to come first."""
    poster = env["presets"] / "diagrams" / ".agents" / "skills" / "poster"
    poster.mkdir(parents=True)
    (poster / "SKILL.md").write_text(
        "---\nname: poster\ndescription: Layout and design for print posters.\n---\n",
        encoding="utf-8",
    )

    found = [r["skill"] for r in rows(env, "design")]
    assert found[0] == "web-design", found
    assert "poster" in found, "the description match still belongs in the results"


def fake_fzf(tmp_path, picks, exit_code=0):
    """A stand-in for fzf: reads the candidate lines, echoes back the chosen ones."""
    bin_dir = tmp_path / "bin"
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
            '  printf "%s\\n" "$input" | grep "$want" \n'
            "done\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return bin_dir


def test_fzf_is_given_previewable_lines(env, monkeypatch):
    """The preview pane reads {5}, so every line's fifth field must be a real path."""
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import picker as picker_mod
    from skill_ctl import search as search_mod

    seen = {}

    class Result:
        returncode = 0
        stdout = ""

    def fake_run(cmd, input=None, **kwargs):
        seen["cmd"], seen["input"] = cmd, input
        return Result()

    monkeypatch.setattr(picker_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        picker_mod.shutil,
        "which",
        lambda name: "/usr/bin/fzf" if name == "fzf" else None,
    )
    monkeypatch.setattr(search_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr(picker_mod, "load_config", lambda: {"theme": "default"})
    monkeypatch.chdir(env["proj"])

    monkeypatch.setattr(search_mod.sys.stdin, "isatty", lambda: True)
    search_mod.search(project=str(env["proj"]))

    from skill_ctl.theme import picker_ansi

    colors = picker_ansi("default")

    assert "--preview" in seen["cmd"]
    expected_preview = r"type {5}\SKILL.md" if os.name == "nt" else "cat {5}/SKILL.md"
    assert seen["cmd"][seen["cmd"].index("--preview") + 1] == expected_preview
    assert "--multi" in seen["cmd"], "Tab-selecting several is the point"
    for line in seen["input"].splitlines():
        fields = line.split("\t")
        assert len(fields) == 5, fields
        assert fields[0].startswith(colors["name"]) and fields[0].endswith(colors["reset"])
        assert fields[1].startswith(colors["location"]) and fields[1].endswith(colors["reset"])
        assert fields[2].startswith(colors["updated"]) and fields[2].endswith(colors["reset"])
        assert fields[3].startswith(colors["description"]) and fields[3].endswith(colors["reset"])
        assert (Path(fields[4]) / "SKILL.md").is_file(), fields[4]


@pytest.mark.parametrize(
    "installed, expected",
    [
        ({"bat", "glow"}, "bat "),
        ({"batcat", "glow"}, "batcat "),
        ({"bat"}, "bat "),
        ({"batcat"}, "batcat "),
        ({"glow"}, "glow "),
        (set(), "type " if os.name == "nt" else "cat "),
    ],
)
def test_preview_uses_the_best_installed_renderer(monkeypatch, installed, expected):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import picker as picker_mod

    monkeypatch.setattr(
        picker_mod.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in installed else None,
    )

    command = picker_mod.preview_command()
    assert command.startswith(expected)
    if expected in ("bat ", "batcat "):
        assert "--squeeze-blank" in command
        assert "--wrap=character" in command
        if os.name == "nt":
            assert '--terminal-width="%FZF_PREVIEW_COLUMNS%"' in command
        else:
            assert '--terminal-width="$FZF_PREVIEW_COLUMNS"' in command


def test_preview_uses_windows_shell_syntax(monkeypatch):
    sys.path.insert(0, str(REPO_ROOT))
    from skill_ctl import picker as picker_mod

    monkeypatch.setattr(picker_mod, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(picker_mod.shutil, "which", lambda name: None)
    assert picker_mod.preview_command() == r"type {5}\SKILL.md"

    monkeypatch.setattr(picker_mod.shutil, "which", lambda name: name if name == "bat" else None)
    command = picker_mod.preview_command()
    assert '--terminal-width="%FZF_PREVIEW_COLUMNS%"' in command
    assert command.endswith(r"{5}\SKILL.md")


def test_fuzzy_matches_the_name_like_fzf(env):
    """`ltxpap` is not a substring of latex-paper, but it is a subsequence."""
    assert [r["skill"] for r in rows(env, "ltxpap")] == ["latex-paper"]
    assert [r["skill"] for r in rows(env, "wbdsgn")] == ["web-design"]


def test_fuzzy_does_not_apply_to_descriptions(env):
    """A subsequence of a long description matches almost anything, so it must not."""
    assert rows(env, "bblgrphy") == [], "fuzzy matching belongs to names only"
    assert [r["skill"] for r in rows(env, "bibliography")] == ["latex-paper"]


def test_a_tighter_fuzzy_match_ranks_higher(env):
    ranked = [r["skill"] for r in rows(env, "draw")]
    assert ranked[0] == "drawio"


def test_pick_applies_what_was_chosen(env, tmp_path):
    bin_dir = fake_fzf(tmp_path, ["drawio"])
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "search", "--pick"],
        cwd=env["proj"],
        env=dict(env["env"], PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert (env["proj"] / ".agents" / "skills" / "drawio").is_symlink()
    assert (env["proj"] / ".claude" / "skills" / "drawio").is_symlink()
    # And it is recorded, so unapply and --resync know about it.
    record = json.loads((env["proj"] / "skills-applied.json").read_text())
    assert record["presets"]["diagrams"]["skills"] == ["drawio"]


def test_pick_spanning_two_presets_applies_each(env, tmp_path):
    bin_dir = fake_fzf(tmp_path, ["drawio", "latex-paper"])
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "search", "--pick"],
        cwd=env["proj"],
        env=dict(env["env"], PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    record = json.loads((env["proj"] / "skills-applied.json").read_text())["presets"]
    assert sorted(record) == ["diagrams", "frontend"]
    assert record["frontend"]["skills"] == ["latex-paper"]


def test_cancelling_the_picker_changes_nothing(env, tmp_path):
    bin_dir = fake_fzf(tmp_path, [], exit_code=130)  # Esc
    res = subprocess.run(
        [sys.executable, "-m", "skill_ctl.cli", "search", "--pick"],
        cwd=env["proj"],
        env=dict(env["env"], PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "Nothing picked" in flat(res)
    assert not (env["proj"] / ".agents").exists()
    assert not (env["proj"] / "skills-applied.json").exists()


def test_pick_and_json_remains_a_compatible_json_alias(env):
    res = skctl(env, "search", "--pick", "--json")
    assert res.returncode == 0
    assert json.loads(res.stdout)


def test_nothing_matches_is_not_an_error(env):
    res = skctl(env, "search", "kubernetes")
    assert res.returncode == 0, res.stderr
    assert "kubernetes" in flat(res)
    assert "skctl search --remote" in flat(res), "point at the remote search instead"


def test_long_descriptions_are_kept_out_of_json_truncation(env):
    wordy = env["presets"] / "diagrams" / ".agents" / "skills" / "wordy"
    wordy.mkdir(parents=True)
    (wordy / "SKILL.md").write_text(
        "---\nname: wordy\ndescription: " + "verbose " * 60 + "\n---\n", encoding="utf-8"
    )
    # --json keeps the full text for anything that wants it.
    full = [r for r in rows(env, "wordy")][0]["description"]
    assert len(full) > 300


def test_skill_meta_mtime_cache(tmp_path, monkeypatch):
    import time

    from skill_ctl import picker as picker_mod

    cache_file = tmp_path / ".metadata_cache.json"
    monkeypatch.setattr(picker_mod, "METADATA_CACHE_FILE", cache_file)
    monkeypatch.setattr(picker_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(picker_mod, "_METADATA_CACHE", None)
    monkeypatch.setattr(picker_mod, "_METADATA_CACHE_DIRTY", False)

    skill_dir = tmp_path / "myskill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: initial-name\ndescription: initial desc\n---\n")

    name, desc, _ = picker_mod.skill_meta(skill_dir)
    assert name == "initial-name"
    assert desc == "initial desc"

    picker_mod._save_metadata_cache()
    assert cache_file.is_file()

    time.sleep(0.01)
    skill_file.write_text("---\nname: updated-name\ndescription: updated desc\n---\n")
    name2, desc2, _ = picker_mod.skill_meta(skill_dir)
    assert name2 == "updated-name"
    assert desc2 == "updated desc"


def test_parse_github_repo_helper():
    from skill_ctl.catalog import parse_github_repo

    assert (
        parse_github_repo("https://github.com/affaan-m/ECC/tree/main/skills/react-patterns")
        == "affaan-m/ECC"
    )
    assert (
        parse_github_repo("https://github.com/vercel-labs/agent-skills.git")
        == "vercel-labs/agent-skills"
    )
    assert parse_github_repo("git@github.com:anthropics/skills.git") == "anthropics/skills"
    assert parse_github_repo("owner/repo") == "owner/repo"
    assert parse_github_repo(None) is None


def test_multi_remote_search_merges_and_deduplicates(monkeypatch):
    from skill_ctl import catalog

    def mock_get_json(url, timeout=10):
        if "skills.sh" in url:
            return {
                "skills": [
                    {
                        "name": "react-patterns",
                        "source": "affaan-m/ECC",
                        "installs": 1500,
                        "id": "affaan-m/ECC/react-patterns",
                    },
                    {
                        "name": "vercel-optimize",
                        "source": "vercel-labs/agent-skills",
                        "installs": 5000,
                        "id": "vercel-labs/agent-skills/vercel-optimize",
                    },
                ]
            }
        elif "skillsmp.com" in url:
            return {
                "success": True,
                "data": {
                    "skills": [
                        {
                            "name": "react-patterns",
                            "author": "affaan-m",
                            "githubUrl": "https://github.com/affaan-m/ECC/tree/main/skills/react-patterns",
                            "stars": 260000,
                            "description": "React patterns and hooks",
                            "id": "skillsmp-react-patterns",
                        },
                        {
                            "name": "setup-ts",
                            "author": "mattpocock",
                            "githubUrl": "https://github.com/mattpocock/skills/tree/main/skills/setup-ts",
                            "stars": 10000,
                            "description": "TypeScript setup",
                            "id": "skillsmp-setup-ts",
                        },
                    ]
                },
            }
        return {}

    monkeypatch.setattr(catalog, "get_json", mock_get_json)

    # Search all remotes
    results = catalog.search("react", remotes=["all"], min_relevance=0)
    assert len(results) == 3

    # react-patterns should be merged across skills.sh and skillsmp
    merged = next(r for r in results if r["name"] == "react-patterns")
    assert merged["source"] == "affaan-m/ECC"
    assert merged["installs"] == 1500
    assert merged["stars"] == 260000
    assert "skills.sh" in merged["remote"] and "skillsmp" in merged["remote"]
    assert merged["description"] == "React patterns and hooks"


def test_remote_filter_skillsmp_only(monkeypatch):
    from skill_ctl import catalog

    calls = []

    def mock_get_json(url, timeout=10):
        calls.append(url)
        if "skillsmp.com" in url:
            return {
                "success": True,
                "data": {
                    "skills": [
                        {
                            "name": "skillsmp-only",
                            "author": "auth",
                            "githubUrl": "https://github.com/auth/repo",
                            "stars": 50,
                            "id": "1",
                        }
                    ]
                },
            }
        return {"skills": []}

    monkeypatch.setattr(catalog, "get_json", mock_get_json)

    results = catalog.search("skill", remotes=["skillsmp"])
    assert len(results) == 1
    assert results[0]["name"] == "skillsmp-only"
    assert any("skillsmp.com" in c for c in calls)
    assert not any("skills.sh" in c for c in calls)


def test_remote_disabled_by_default_but_overridden_by_flag(monkeypatch):
    from skill_ctl import catalog

    calls = []

    def mock_get_json(url, timeout=10):
        calls.append(url)
        if "skillsmp.com" in url:
            return {
                "data": {
                    "skills": [
                        {
                            "name": "skillsmp-skill",
                            "author": "auth",
                            "githubUrl": "https://github.com/auth/repo",
                            "stars": 10,
                            "id": "2",
                        }
                    ]
                }
            }
        elif "skills.sh" in url:
            return {
                "skills": [
                    {"name": "skillssh-skill", "source": "auth/repo", "installs": 10, "id": "1"}
                ]
            }
        return {}

    monkeypatch.setattr(catalog, "get_json", mock_get_json)

    # 1. Default (skillsmp disabled): only queries skills.sh
    res_default = catalog.search("skill")
    assert len(res_default) == 1
    assert res_default[0]["name"] == "skillssh-skill"
    assert not any("skillsmp.com" in c for c in calls)

    calls.clear()
    # 2. remotes=["skillsmp"]: queries skillsmp explicitly
    res_skillsmp = catalog.search("skill", remotes=["skillsmp"])
    assert len(res_skillsmp) == 1
    assert res_skillsmp[0]["name"] == "skillsmp-skill"
    assert any("skillsmp.com" in c for c in calls)

    calls.clear()
    # 3. remotes=["all"]: queries both
    res_all = catalog.search("skill", remotes=["all"])
    assert len(res_all) == 2
    assert any("skillsmp.com" in c for c in calls)
    assert any("skills.sh" in c for c in calls)


def test_cli_search_remote_preprocess():
    from skill_ctl.cli import preprocess_args

    assert preprocess_args(["search", "--remote"]) == ["search", "--remote", "default"]
    assert preprocess_args(["search", "-r"]) == ["search", "-r", "default"]
    assert preprocess_args(["search", "--remote", "skillsmp"]) == ["search", "--remote", "skillsmp"]
    assert preprocess_args(["search", "react", "--remote", "skills.sh"]) == [
        "search",
        "react",
        "--remote",
        "skills.sh",
    ]
    assert preprocess_args(["search", "react", "--remote", "all"]) == [
        "search",
        "react",
        "--remote",
        "all",
    ]


def test_format_relative_time():
    from datetime import datetime, timedelta, timezone

    from skill_ctl.catalog import format_relative_time

    now = datetime.now(timezone.utc)
    one_hour_ago = (now - timedelta(hours=1)).isoformat()
    two_days_ago = (now - timedelta(days=2)).isoformat()

    res_1h = format_relative_time(one_hour_ago)
    assert "1h ago" in res_1h
    assert format_relative_time(one_hour_ago, short=True) == "1h ago"

    res_2d = format_relative_time(two_days_ago)
    assert "2d ago" in res_2d
    assert format_relative_time(two_days_ago, short=True) == "2d ago"

    # Unix timestamp
    ts = int((now - timedelta(days=5)).timestamp())
    res_ts = format_relative_time(ts)
    assert "5d ago" in res_ts
    assert format_relative_time(ts, short=True) == "5d ago"

    assert format_relative_time(None) is None
    assert format_relative_time("") is None


def test_catalog_format_row_includes_updated():
    from skill_ctl.catalog import format_row

    skill = {
        "name": "test-skill",
        "source": "owner/repo",
        "installs": 1200,
        "stars": 45,
        "updated_at": "2026-08-20T12:00:00Z",
        "remote": "skillsmp",
    }
    row_text = format_row(skill)
    assert "test-skill" in row_text
    assert "owner/repo" in row_text
    assert "skillsmp" in row_text
    # Should include formatted metric and updated column
    assert "★ 45" in row_text or "↓" in row_text


def test_details_fetches_and_formats_updated_time(monkeypatch):
    from skill_ctl import catalog

    def mock_get_json(url, timeout=10):
        if "api.github.com/repos/owner/repo" in url:
            return {
                "stargazers_count": 1234,
                "pushed_at": "2026-08-20T12:00:00Z",
            }
        return {}

    monkeypatch.setattr(catalog, "get_json", mock_get_json)
    monkeypatch.setattr(catalog, "frontmatter", lambda source, name: {"description": "test"})

    info = catalog.details(
        {"name": "test-skill", "source": "owner/repo", "remote": "skills.sh", "installs": 100}
    )
    assert info["stars"] == 1234
    assert info["updated_at"] == "2026-08-20T12:00:00Z"
    assert "2026-08-20" in info["updated"]

    lines = catalog.detail_lines(info)
    assert any(line.startswith("Updated: ") for line in lines)


def test_skill_meta_extracts_frontmatter_date(tmp_path):
    from skill_ctl.picker import parse_skill_meta, skill_meta

    skill_dir = tmp_path / "dated-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: dated-skill\ndescription: Has explicit updated date\nupdated: 2026-07-15\n---\n# Content\n",
        encoding="utf-8",
    )
    name, desc, updated = parse_skill_meta(skill_dir)
    assert name == "dated-skill"
    assert desc == "Has explicit updated date"
    assert updated == "2026-07-15"

    name_m, desc_m, updated_m = skill_meta(skill_dir)
    assert name_m == "dated-skill"
    assert updated_m == "2026-07-15"


def test_search_json_output_includes_updated_at(env):
    found = rows(env, "latex")
    assert len(found) == 1
    assert "updated_at" in found[0]
    assert found[0]["updated_at"] is not None


def test_search_remote_install_triggers_maybe_auto_push(monkeypatch, tmp_path):
    import sys

    from skill_ctl import search as search_mod

    pushed = []
    monkeypatch.setattr(search_mod, "maybe_auto_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr(search_mod, "run_npx_skills", lambda args, cwd: 0)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "skill_ctl.catalog.choose",
        lambda query="", owner=None, remotes=None: [
            {"source": "owner/repo1", "name": "skill-1"},
            {"source": "owner/repo2", "name": "skill-2"},
        ],
    )
    monkeypatch.setattr("skill_ctl.catalog.inspect", lambda skills: True)
    search_mod.search(preset="my-preset", remote_only=True)
    assert len(pushed) == 1
    assert "add skill-1, skill-2 to my-preset" in pushed[0]


def test_picker_fzf_actions(monkeypatch):
    from skill_ctl import picker as picker_mod

    rows = [
        {
            "skill": "s1",
            "location": "p1",
            "updated": "now",
            "description": "d1",
            "path": "/path/to/s1",
        },
        {
            "skill": "s2",
            "location": "p2",
            "updated": "now",
            "description": "d2",
            "path": "/path/to/s2",
        },
    ]

    monkeypatch.setattr(picker_mod.shutil, "which", lambda name: "/usr/bin/fzf")
    monkeypatch.setattr(picker_mod, "load_config", lambda: {})

    # 1. Alt-P
    class ResultAltP:
        returncode = 0
        stdout = "alt-p\n" + "\t".join(["s1", "p1", "now", "d1", "/path/to/s1"]) + "\n"

    monkeypatch.setattr(picker_mod.subprocess, "run", lambda *args, **kwargs: ResultAltP())
    chosen, ok = picker_mod.pick_with_fzf(rows)
    assert ok is True
    assert len(chosen) == 1
    assert chosen[0]["skill"] == "s1"
    assert chosen[0]["_action"] == "alt-p"

    # 2. Alt-G
    class ResultAltG:
        returncode = 0
        stdout = "alt-g\n" + "\t".join(["s2", "p2", "now", "d2", "/path/to/s2"]) + "\n"

    monkeypatch.setattr(picker_mod.subprocess, "run", lambda *args, **kwargs: ResultAltG())
    chosen, ok = picker_mod.pick_with_fzf(rows)
    assert ok is True
    assert len(chosen) == 1
    assert chosen[0]["skill"] == "s2"
    assert chosen[0]["_action"] == "alt-g"

    # 3. Alt-X
    class ResultAltX:
        returncode = 0
        stdout = "alt-x\n" + "\t".join(["s1", "p1", "now", "d1", "/path/to/s1"]) + "\n"

    monkeypatch.setattr(picker_mod.subprocess, "run", lambda *args, **kwargs: ResultAltX())
    chosen, ok = picker_mod.pick_with_fzf(rows)
    assert ok is True
    assert len(chosen) == 1
    assert chosen[0]["skill"] == "s1"
    assert chosen[0]["_action"] == "alt-x"

    # 4. Alt-A (destination action)
    class ResultAltA:
        returncode = 0
        stdout = "alt-a\n" + "\t".join(["s1", "p1", "now", "d1", "/path/to/s1"]) + "\n"

    monkeypatch.setattr(picker_mod.subprocess, "run", lambda *args, **kwargs: ResultAltA())
    chosen, ok = picker_mod.pick_with_fzf(rows)
    assert ok is True
    assert len(chosen) == 1
    assert chosen[0]["skill"] == "s1"
    assert chosen[0]["_action"] == "alt-a"

    # 5. Enter (default action)
    class ResultEnter:
        returncode = 0
        stdout = "\n" + "\t".join(["s1", "p1", "now", "d1", "/path/to/s1"]) + "\n"

    monkeypatch.setattr(picker_mod.subprocess, "run", lambda *args, **kwargs: ResultEnter())
    chosen, ok = picker_mod.pick_with_fzf(rows)
    assert ok is True
    assert len(chosen) == 1
    assert chosen[0]["skill"] == "s1"
    assert chosen[0]["_action"] is None


def test_catalog_fzf_actions(tmp_path, monkeypatch):
    from skill_ctl import catalog as catalog_mod

    meta_file = tmp_path / "skill.json"
    meta_file.write_text(json.dumps({"name": "test-remote", "source": "owner/repo"}))

    monkeypatch.setattr(catalog_mod.shutil, "which", lambda name: "/usr/bin/fzf")
    monkeypatch.setattr(catalog_mod, "load_config", lambda: {})
    monkeypatch.setattr(catalog_mod, "read_preview", lambda path: "")

    # Alt-P
    class ResultAltP:
        returncode = 0
        stdout = f"alt-p\nformatted_row\t{meta_file}\n"

    monkeypatch.setattr(catalog_mod.subprocess, "run", lambda *args, **kwargs: ResultAltP())
    chosen = catalog_mod.choose_with_fzf("query")
    assert len(chosen) == 1
    assert chosen[0]["name"] == "test-remote"
    assert chosen[0]["_action"] == "alt-p"

    # Alt-G
    class ResultAltG:
        returncode = 0
        stdout = f"alt-g\nformatted_row\t{meta_file}\n"

    monkeypatch.setattr(catalog_mod.subprocess, "run", lambda *args, **kwargs: ResultAltG())
    chosen = catalog_mod.choose_with_fzf("query")
    assert len(chosen) == 1
    assert chosen[0]["name"] == "test-remote"
    assert chosen[0]["_action"] == "alt-g"

    # Alt-A (all from repo)
    class ResultAltA:
        returncode = 0
        stdout = f"alt-a\nformatted_row\t{meta_file}\n"

    monkeypatch.setattr(catalog_mod.subprocess, "run", lambda *args, **kwargs: ResultAltA())
    chosen = catalog_mod.choose_with_fzf("query")
    assert len(chosen) == 1
    assert chosen[0]["_all_from_source"] is True
    assert chosen[0]["_action"] == "alt-a"


def test_fetch_repo_skills_github_tree(monkeypatch):
    from skill_ctl import catalog as catalog_mod

    def mock_get_json(url, timeout=3.0):
        if "trees" in url:
            return {
                "tree": [
                    {"path": "skills/review/SKILL.md", "type": "blob"},
                    {"path": "skills/tdd/SKILL.md", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ]
            }
        return {}

    monkeypatch.setattr(catalog_mod, "get_json", mock_get_json)
    skills = catalog_mod.fetch_repo_skills("owner/repo")
    assert skills == ["review", "tdd"]


def test_catalog_alt_a_prompts_and_picks_repo_skills(tmp_path, monkeypatch):
    from skill_ctl import catalog as catalog_mod

    meta_file = tmp_path / "skill.json"
    meta_file.write_text(json.dumps({"name": "review", "source": "owner/repo"}))

    monkeypatch.setattr(catalog_mod.shutil, "which", lambda name: "/usr/bin/fzf")
    monkeypatch.setattr(catalog_mod, "load_config", lambda: {})
    monkeypatch.setattr(catalog_mod, "read_preview", lambda path: "")

    monkeypatch.setattr(catalog_mod, "fetch_repo_skills", lambda src: ["review", "tdd", "lint"])
    monkeypatch.setattr(catalog_mod, "choose_repo_skills", lambda src, skills: ["review", "tdd"])

    class ResultAltA:
        returncode = 0
        stdout = f"alt-a\nformatted_row\t{meta_file}\n"

    monkeypatch.setattr(catalog_mod.subprocess, "run", lambda *args, **kwargs: ResultAltA())
    chosen = catalog_mod.choose_with_fzf("query")
    assert len(chosen) == 2
    assert chosen[0]["name"] == "review"
    assert chosen[0]["source"] == "owner/repo"
    assert chosen[0]["_action"] == "alt-a"
    assert chosen[1]["name"] == "tdd"
    assert chosen[1]["source"] == "owner/repo"
    assert chosen[1]["_action"] == "alt-a"


def test_search_remote_alt_p_prompts_and_installs_preset(monkeypatch, tmp_path):
    from skill_ctl import search as search_mod

    pushed = []
    npx_calls = []
    monkeypatch.setattr(search_mod, "maybe_auto_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr(
        search_mod, "run_npx_skills", lambda args, cwd: npx_calls.append((args, cwd)) or 0
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "skill_ctl.catalog.choose",
        lambda query="", owner=None, remotes=None: [
            {"source": "owner/repo", "name": "skill-1", "_action": "alt-p"},
        ],
    )
    monkeypatch.setattr("skill_ctl.catalog.inspect", lambda skills: True)
    monkeypatch.setattr(
        "skill_ctl.prompts.prompt_preset_or_new", lambda *args, **kwargs: "chosen-preset"
    )

    presets_dir = tmp_path / "presets"
    monkeypatch.setattr(search_mod, "PRESETS_DIR", presets_dir)

    search_mod.search(remote_only=True)

    assert len(npx_calls) == 1
    args, cwd = npx_calls[0]
    assert "--skill" in args and "skill-1" in args
    assert "-g" not in args
    assert "chosen-preset" in cwd
    assert len(pushed) == 1
    assert "add skill-1 to chosen-preset" in pushed[0]


def test_search_remote_alt_g_installs_globally(monkeypatch):
    from rich.prompt import Confirm

    from skill_ctl import search as search_mod

    npx_calls = []
    monkeypatch.setattr(
        search_mod, "run_npx_skills", lambda args, cwd: npx_calls.append((args, cwd)) or 0
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "skill_ctl.catalog.choose",
        lambda query="", owner=None, remotes=None: [
            {"source": "owner/repo", "name": "skill-global", "_action": "alt-g"},
        ],
    )
    monkeypatch.setattr("skill_ctl.catalog.inspect", lambda skills: True)

    search_mod.search(remote_only=True)

    assert len(npx_calls) == 1
    args, cwd = npx_calls[0]
    assert "-g" in args
    assert cwd == str(Path.home())


def test_search_remote_alt_g_cancelled(monkeypatch):
    from rich.prompt import Confirm

    from skill_ctl import search as search_mod

    npx_calls = []
    monkeypatch.setattr(
        search_mod, "run_npx_skills", lambda args, cwd: npx_calls.append((args, cwd)) or 0
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "skill_ctl.catalog.choose",
        lambda query="", owner=None, remotes=None: [
            {"source": "owner/repo", "name": "skill-global", "_action": "alt-g"},
        ],
    )
    monkeypatch.setattr("skill_ctl.catalog.inspect", lambda skills: True)

    search_mod.search(remote_only=True)

    assert len(npx_calls) == 0


def test_add_installed_to_global_confirmation_and_cancel(env, monkeypatch):
    from rich.prompt import Confirm
    from skill_ctl.presets import add_installed_to_global

    monkeypatch.setattr("skill_ctl.presets.PRESETS_DIR", env["presets"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    # If user cancels:
    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: False)
    added = add_installed_to_global({}, skill="drawio")
    assert added == 0

    # If user confirms:
    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)
    added = add_installed_to_global({}, skill="drawio")
    assert added >= 1


def test_search_local_alt_p_installs_into_preset(env, monkeypatch):
    from skill_ctl import search as search_mod

    pushed = []
    monkeypatch.setattr(search_mod, "maybe_auto_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr(search_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.presets.PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.constants.PRESETS_DIR", env["presets"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    chosen_skill = {
        "skill": "drawio",
        "preset": "diagrams",
        "scope": "preset:diagrams",
        "path": str(env["presets"] / "diagrams" / ".agents" / "skills" / "drawio"),
        "description": "Create diagrams",
        "location": "diagrams",
        "updated": "now",
        "_action": "alt-p",
    }

    monkeypatch.setattr(
        search_mod, "pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick_with_fzf", lambda rows, header=None, query="": ([chosen_skill], True)
    )
    monkeypatch.setattr(
        "skill_ctl.prompts.prompt_preset_or_new",
        lambda names, default_p, presets_dir, **kwargs: "target-preset",
    )

    search_mod.search(project=str(env["proj"]))

    target_skill_dir = env["presets"] / "target-preset" / ".agents" / "skills" / "drawio"
    assert target_skill_dir.exists()
    assert (target_skill_dir / "SKILL.md").exists()
    assert len(pushed) == 1
    assert "add drawio to target-preset" in pushed[0]


def test_search_local_alt_g_adds_globally(env, monkeypatch):
    from skill_ctl import search as search_mod

    global_added = []
    monkeypatch.setattr(
        "skill_ctl.search.add_installed_to_global", lambda cfg, skill: global_added.append(skill)
    )
    monkeypatch.setattr(search_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.presets.PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.constants.PRESETS_DIR", env["presets"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    chosen_skill = {
        "skill": "latex-paper",
        "preset": "frontend",
        "scope": "preset:frontend",
        "path": str(env["presets"] / "frontend" / ".agents" / "skills" / "latex-paper"),
        "description": "LaTeX papers",
        "location": "frontend",
        "updated": "now",
        "_action": "alt-g",
    }

    monkeypatch.setattr(
        search_mod, "pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick_with_fzf", lambda rows, header=None, query="": ([chosen_skill], True)
    )

    search_mod.search(project=str(env["proj"]))

    assert len(global_added) == 1
    assert global_added[0] == "latex-paper"


def test_search_local_alt_x_deletes_skill(env, monkeypatch):
    from skill_ctl import search as search_mod

    pushed = []
    monkeypatch.setattr(search_mod, "maybe_auto_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr(search_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.presets.PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.constants.PRESETS_DIR", env["presets"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *args, **kwargs: True)

    skill_path = env["presets"] / "frontend" / ".agents" / "skills" / "latex-paper"
    assert skill_path.is_dir()

    chosen_skill = {
        "skill": "latex-paper",
        "preset": "frontend",
        "scope": "preset",
        "path": str(skill_path),
        "description": "LaTeX papers",
        "location": "frontend",
        "updated": "now",
        "_action": "alt-x",
    }

    monkeypatch.setattr(
        search_mod, "pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick_with_fzf", lambda rows, header=None, query="": ([chosen_skill], True)
    )

    search_mod.search(project=str(env["proj"]))

    assert not skill_path.exists()
    assert any("delete skills from frontend" in p for p in pushed)


def test_search_local_alt_a_prompts_destination_and_applies_project(env, monkeypatch):
    from skill_ctl import search as search_mod

    applied = []
    monkeypatch.setattr(
        "skill_ctl.search.apply_picked",
        lambda chosen, project=None: applied.append((chosen, project)),
    )
    monkeypatch.setattr(
        "skill_ctl.prompts.prompt_destination", lambda *args, **kwargs: (None, True, False)
    )
    monkeypatch.setattr(search_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.presets.PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.constants.PRESETS_DIR", env["presets"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    chosen_skill = {
        "skill": "drawio",
        "preset": "diagrams",
        "scope": "preset:diagrams",
        "path": str(env["presets"] / "diagrams" / ".agents" / "skills" / "drawio"),
        "description": "Create diagrams",
        "location": "diagrams",
        "updated": "now",
        "_action": "alt-a",
    }
    monkeypatch.setattr(
        search_mod, "pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick_with_fzf", lambda rows, header=None, query="": ([chosen_skill], True)
    )

    search_mod.search(project=str(env["proj"]))

    assert len(applied) == 1
    assert applied[0][0][0]["skill"] == "drawio"
    assert applied[0][1] == str(env["proj"])


def test_search_local_alt_a_prompts_destination_and_installs_preset(env, monkeypatch):
    from skill_ctl import search as search_mod

    pushed = []
    monkeypatch.setattr(search_mod, "maybe_auto_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr(search_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.presets.PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.constants.PRESETS_DIR", env["presets"])
    monkeypatch.setattr(
        "skill_ctl.prompts.prompt_destination",
        lambda *args, **kwargs: ("target-preset", False, False),
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    chosen_skill = {
        "skill": "drawio",
        "preset": "diagrams",
        "scope": "preset:diagrams",
        "path": str(env["presets"] / "diagrams" / ".agents" / "skills" / "drawio"),
        "description": "Create diagrams",
        "location": "diagrams",
        "updated": "now",
        "_action": "alt-a",
    }
    monkeypatch.setattr(
        search_mod, "pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick_with_fzf", lambda rows, header=None, query="": ([chosen_skill], True)
    )

    search_mod.search(project=str(env["proj"]))

    target_skill_dir = env["presets"] / "target-preset" / ".agents" / "skills" / "drawio"
    assert target_skill_dir.exists()
    assert len(pushed) == 1
    assert "add drawio to target-preset" in pushed[0]


def test_search_local_alt_a_prompts_destination_and_installs_global(env, monkeypatch):
    from skill_ctl import search as search_mod

    global_added = []
    monkeypatch.setattr(
        "skill_ctl.search.add_installed_to_global", lambda cfg, skill: global_added.append(skill)
    )
    monkeypatch.setattr(
        "skill_ctl.prompts.prompt_destination", lambda *args, **kwargs: (None, False, True)
    )
    monkeypatch.setattr(search_mod, "PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.presets.PRESETS_DIR", env["presets"])
    monkeypatch.setattr("skill_ctl.constants.PRESETS_DIR", env["presets"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    chosen_skill = {
        "skill": "drawio",
        "preset": "diagrams",
        "scope": "preset:diagrams",
        "path": str(env["presets"] / "diagrams" / ".agents" / "skills" / "drawio"),
        "description": "Create diagrams",
        "location": "diagrams",
        "updated": "now",
        "_action": "alt-a",
    }
    monkeypatch.setattr(
        search_mod, "pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick", lambda rows, header=None, action="apply", query="": [chosen_skill]
    )
    monkeypatch.setattr(
        "skill_ctl.picker.pick_with_fzf", lambda rows, header=None, query="": ([chosen_skill], True)
    )

    search_mod.search(project=str(env["proj"]))

    assert len(global_added) == 1
    assert global_added[0] == "drawio"


def test_search_remote_alt_a_prompts_destination_and_installs_preset(monkeypatch, tmp_path):
    from skill_ctl import search as search_mod

    npx_calls = []
    monkeypatch.setattr(
        search_mod, "run_npx_skills", lambda args, cwd: npx_calls.append((args, cwd)) or 0
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "skill_ctl.catalog.choose",
        lambda query="", owner=None, remotes=None, **kwargs: [
            {
                "source": "owner/repo",
                "name": "all skills",
                "_all_from_source": True,
                "_action": "alt-a",
            },
        ],
    )
    monkeypatch.setattr("skill_ctl.catalog.inspect", lambda skills: True)
    monkeypatch.setattr(
        "skill_ctl.prompts.prompt_destination", lambda *args, **kwargs: ("my-preset", False, False)
    )

    presets_dir = tmp_path / "presets"
    monkeypatch.setattr(search_mod, "PRESETS_DIR", presets_dir)

    search_mod.search(remote_only=True)

    assert len(npx_calls) == 1
    args, cwd = npx_calls[0]
    assert args[0:2] == ["add", "owner/repo"]
    assert "--agent" in args and "universal" in args
    assert "-y" in args
    assert "my-preset" in cwd


def test_collect_multiple_presets(env):
    res_rows = rows(env, "--preset", "frontend, diagrams")
    presets_found = {r["preset"] for r in res_rows}
    assert "frontend" in presets_found
    assert "diagrams" in presets_found


def test_config_search_limit(monkeypatch):
    from skill_ctl.config import DEFAULT_SEARCH_LIMIT, load_config

    # Default
    monkeypatch.setattr("skill_ctl.config.read_config_file", lambda: {})
    assert load_config()["search_limit"] == DEFAULT_SEARCH_LIMIT == 50

    # Custom
    monkeypatch.setattr("skill_ctl.config.read_config_file", lambda: {"search_limit": 100})
    assert load_config()["search_limit"] == 100

    # Alias search_query_limit
    monkeypatch.setattr("skill_ctl.config.read_config_file", lambda: {"search_query_limit": 75})
    assert load_config()["search_limit"] == 75

    # Invalid fallback
    monkeypatch.setattr("skill_ctl.config.read_config_file", lambda: {"search_limit": "invalid"})
    assert load_config()["search_limit"] == 50


def test_catalog_get_search_limit(monkeypatch):
    from skill_ctl.catalog import get_search_limit

    # Override takes precedence
    assert get_search_limit(override=120) == 120

    # Falls back to config
    monkeypatch.setattr("skill_ctl.config.read_config_file", lambda: {"search_limit": 80})
    assert get_search_limit() == 80


def test_catalog_remote_search_limits(monkeypatch):
    from skill_ctl import catalog

    requested_urls = []

    def mock_get_json(url, timeout=10):
        requested_urls.append(url)
        return {"skills": [], "data": []}

    monkeypatch.setattr(catalog, "get_json", mock_get_json)

    # skills.sh gets requested limit
    catalog.search_skills_sh("presenter", limit=150)
    assert any("limit=150" in u for u in requested_urls)

    requested_urls.clear()

    # skillsmp.com is clamped to 50 max to prevent HTTP 400
    catalog.search_skillsmp("presenter", limit=150)
    assert any("limit=50" in u for u in requested_urls)


def test_search_cli_limit_option(monkeypatch):
    from skill_ctl import search as search_mod

    called_kwargs = {}

    def mock_catalog_search(query="", owner=None, remotes=None, **kwargs):
        called_kwargs.update(kwargs)
        return []

    monkeypatch.setattr("skill_ctl.catalog.search", mock_catalog_search)

    search_mod.search(query="presenter", remote="skills.sh", limit=99, json_output=True)
    assert called_kwargs.get("limit") == 99
