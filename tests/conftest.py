import os

import pytest

# Ensure rich console and subprocesses do not wrap paths on narrow terminals (e.g. pytest-xdist workers)
os.environ["COLUMNS"] = "200"


@pytest.fixture(autouse=True)
def _isolated_catalog_cache(tmp_path, monkeypatch):
    """Keep tests that mock the registries from writing into the real ~/.skill-ctl/cache."""
    from skill_ctl import catalog

    cache_root = tmp_path / "catalog-cache"
    monkeypatch.setattr(catalog, "QUERY_CACHE_DIR", cache_root / "queries")
    monkeypatch.setattr(catalog, "REPO_CACHE_DIR", cache_root / "repos")
    monkeypatch.setattr(catalog, "SKILL_CACHE_DIR", cache_root / "skills")
