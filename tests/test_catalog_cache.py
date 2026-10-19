"""Unit tests for remote catalog caching, lazy loading, and fast-paths."""

import time

import pytest

from skill_ctl import catalog
from skill_ctl.catalog import (
    _read_disk_cache,
    _write_disk_cache,
    details,
    frontmatter,
    search,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(catalog, "QUERY_CACHE_DIR", cache_root / "queries")
    monkeypatch.setattr(catalog, "REPO_CACHE_DIR", cache_root / "repos")
    monkeypatch.setattr(catalog, "SKILL_CACHE_DIR", cache_root / "skills")
    return cache_root


def test_disk_cache_read_write(tmp_path):
    target = tmp_path / "test.json"
    _write_disk_cache(target, {"foo": "bar"})
    assert target.exists()
    assert _read_disk_cache(target, ttl=100) == {"foo": "bar"}

    # Test expired TTL
    time.sleep(0.05)
    assert _read_disk_cache(target, ttl=0.01) is None


def test_search_persists_to_disk_and_hits_cache(monkeypatch):
    calls = []

    def mock_get_json(url, timeout=10):
        calls.append(url)
        return {
            "skills": [{"name": "fast-skill", "source": "owner/repo", "installs": 50, "id": "1"}]
        }

    # Force using cache with standard function pointer
    monkeypatch.setattr(catalog, "_DEFAULT_GET_JSON", mock_get_json)
    monkeypatch.setattr(catalog, "get_json", mock_get_json)

    # First search: hits mock network
    res1 = search("fast-skill")
    assert len(res1) == 1
    assert res1[0]["name"] == "fast-skill"
    assert len(calls) >= 1

    calls.clear()
    # Second search: hits disk cache without calling mock network
    res2 = search("fast-skill")
    assert len(res2) == 1
    assert res2[0]["name"] == "fast-skill"
    assert len(calls) == 0


def test_search_falls_back_to_stale_cache_on_error(monkeypatch):
    # Pre-populate disk cache
    cache_key = catalog.query_cache_key("stale-query")
    cache_file = catalog.QUERY_CACHE_DIR / f"{cache_key}.json"
    _write_disk_cache(cache_file, [{"name": "stale-cached-skill", "source": "owner/repo"}])

    # Mock network that always fails
    def failing_get_json(url, timeout=10):
        raise RuntimeError("Network offline")

    monkeypatch.setattr(catalog, "_DEFAULT_GET_JSON", failing_get_json)
    monkeypatch.setattr(catalog, "get_json", failing_get_json)

    # Force cache expiry so it tries network
    monkeypatch.setattr(catalog, "QUERY_CACHE_TTL", 0.0001)
    time.sleep(0.01)

    results = search("stale-query")
    assert len(results) == 1
    assert results[0]["name"] == "stale-cached-skill"


def test_repo_metadata_caching_in_details(monkeypatch):
    gh_calls = []

    def mock_get_json(url, timeout=10):
        gh_calls.append(url)
        if "api.github.com/repos/cached-owner/cached-repo" in url:
            return {"stargazers_count": 999, "pushed_at": "2026-09-01T00:00:00Z"}
        return {}

    monkeypatch.setattr(catalog, "_DEFAULT_GET_JSON", mock_get_json)
    monkeypatch.setattr(catalog, "get_json", mock_get_json)
    monkeypatch.setattr(catalog, "frontmatter", lambda s, n: {})

    skill = {"name": "skill-a", "source": "cached-owner/cached-repo", "remote": "skills.sh"}

    # 1. First details call: hits GitHub API
    info1 = details(skill)
    assert info1["stars"] == 999
    assert len(gh_calls) == 1

    gh_calls.clear()
    # 2. Second details call for same repo: hits repo disk cache
    info2 = details(skill)
    assert info2["stars"] == 999
    assert len(gh_calls) == 0


def test_frontmatter_caching(monkeypatch):
    raw_calls = []

    def mock_fetch_raw(repo, path, branch="main", timeout=3.0):
        raw_calls.append((repo, path, branch))
        return "---\nname: cached-front\ndescription: Caching works\n---\n# Cached"

    monkeypatch.setattr(catalog, "_DEFAULT_GET_JSON", catalog.get_json)
    monkeypatch.setattr(catalog, "_fetch_raw_github_file", mock_fetch_raw)

    # First call: fetches via raw github and parses YAML
    meta1 = frontmatter("user/test-repo", "cached-front")
    assert meta1.get("name") == "cached-front"
    assert meta1.get("description") == "Caching works"
    assert len(raw_calls) >= 1

    raw_calls.clear()
    # Second call: returns cached frontmatter dict instantly
    meta2 = frontmatter("user/test-repo", "cached-front")
    assert meta2.get("name") == "cached-front"
    assert meta2.get("description") == "Caching works"
    assert len(raw_calls) == 0


def test_choose_with_fzf_preloads_initial_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog.shutil, "which", lambda name: "/usr/bin/fzf")
    monkeypatch.setattr(catalog, "load_config", lambda: {})
    monkeypatch.setattr(catalog, "read_preview", lambda path: "")

    captured_input = []

    class DummyResult:
        returncode = 0
        stdout = "skill-row\t/dummy/path\n"

    def mock_run(cmd, input=None, **kwargs):
        captured_input.append(input)
        return DummyResult()

    monkeypatch.setattr(catalog.subprocess, "run", mock_run)
    monkeypatch.setattr(
        catalog, "write_rows", lambda q, c, o, remotes=None: ["row1\tpath1", "row2\tpath2"]
    )

    # Query with >= 3 chars
    catalog.choose_with_fzf("react")
    assert len(captured_input) == 1
    assert captured_input[0] == "row1\tpath1\nrow2\tpath2"

    # Query with < 3 chars should not preload
    captured_input.clear()
    catalog.choose_with_fzf("re")
    assert len(captured_input) == 1
    assert captured_input[0] is None


def test_prune_cache_evicts_old_and_excess_files(tmp_path, monkeypatch):
    cache_root = tmp_path / "prune_test"
    cache_root.mkdir(parents=True)
    monkeypatch.setattr(catalog, "CACHE_DIR", cache_root)

    # 1. Create expired file
    old_file = cache_root / "old.json"
    old_file.write_text("{}", encoding="utf-8")
    old_time = time.time() - (8 * 86400)
    import os

    os.utime(old_file, (old_time, old_time))

    # 2. Create fresh files
    file1 = cache_root / "f1.json"
    file1.write_text("x" * 1000, encoding="utf-8")
    file2 = cache_root / "f2.json"
    file2.write_text("y" * 1000, encoding="utf-8")

    catalog.prune_cache(max_size_bytes=1500, max_age_seconds=7 * 86400, force=True)

    # old_file should be deleted due to age
    assert not old_file.exists()
    # file1 (oldest fresh) should be evicted to bring size below target
    assert not file1.exists()
    # file2 should remain
    assert file2.exists()


def test_clear_cache(tmp_path, monkeypatch):
    cache_root = tmp_path / "clear_test"
    cache_root.mkdir(parents=True)
    monkeypatch.setattr(catalog, "CACHE_DIR", cache_root)

    (cache_root / "a.json").write_text("{}", encoding="utf-8")
    (cache_root / "b.json").write_text("{}", encoding="utf-8")

    cleared = catalog.clear_cache()
    assert cleared == 2
    assert len(list(cache_root.rglob("*.json"))) == 0


def test_get_github_token_from_env(monkeypatch):
    monkeypatch.setattr(catalog, "_GH_TOKEN_CACHE", None)
    monkeypatch.setenv("GITHUB_TOKEN", "token_env_123")
    assert catalog.get_github_token() == "token_env_123"

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "token_gh_env_456")
    assert catalog.get_github_token() == "token_gh_env_456"


def test_get_github_token_from_gh_cli(monkeypatch):
    monkeypatch.setattr(catalog, "_GH_TOKEN_CACHE", None)
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
