"""Relevance ranking and threshold for remote search results."""

from skill_ctl import catalog
from skill_ctl.catalog import rank_results, relevance


def skill(name, source="o/r", description="", installs=0, stars=0):
    return {
        "name": name,
        "source": source,
        "description": description,
        "installs": installs,
        "stars": stars,
    }


def test_name_match_tiers_are_ordered():
    exact = relevance("react", skill("react"))
    prefix = relevance("react", skill("react-hooks"))
    word = relevance("react", skill("vercel-react-best-practices"))
    substring = relevance("react", skill("preact-tools"))
    source_only = relevance("react", skill("hooks", source="facebook/react"))
    description_only = relevance("react", skill("hooks", description="Patterns for React"))
    assert exact > prefix > word > substring > source_only > description_only > 0


def test_unrelated_result_scores_zero():
    assert relevance("react", skill("terraform-modules", source="hashicorp/terraform")) == 0.0


def test_loose_fuzzy_name_match_counts_but_scattered_one_does_not():
    assert relevance("ltxpap", skill("latex-paper")) > 0
    assert relevance("rct", skill("a-reallyyy-long-cool-tool-name")) == 0.0


def test_whole_name_query_with_hyphen_is_exact():
    assert relevance("react-hooks", skill("react-hooks")) == 1.0
    assert 0 < relevance("react-hooks", skill("react-patterns")) < 1.0


def test_results_ordered_by_relevance_then_popularity():
    results = [  # most popular first, as the registry fetch returns them
        skill("vercel-react-best-practices", installs=9000),
        skill("react-native", installs=800),
        skill("react-hooks", installs=500),
        skill("react", installs=10),
    ]
    ranked = [r["name"] for r in rank_results("react", results, min_relevance=0)]
    assert ranked == ["react", "react-native", "react-hooks", "vercel-react-best-practices"]


def test_threshold_drops_weak_results():
    results = [
        skill("react-hooks"),
        skill("hooks", source="facebook/react"),
        skill("terraform", source="hashicorp/terraform"),
    ]

    def names(m):
        return [r["name"] for r in rank_results("react", results, min_relevance=m)]

    assert names(0) == ["react-hooks", "hooks", "terraform"]
    assert names(0.2) == ["react-hooks", "hooks"]
    assert names(0.6) == ["react-hooks"]


def test_threshold_read_from_config(monkeypatch):
    results = [skill("react-hooks"), skill("hooks", source="facebook/react")]
    monkeypatch.setattr(catalog, "load_config", lambda: {"search_min_relevance": 0.6})
    assert [r["name"] for r in rank_results("react", results)] == ["react-hooks"]
    monkeypatch.setattr(catalog, "load_config", lambda: {"search_min_relevance": "bogus"})
    assert len(rank_results("react", results)) == 2


def test_search_applies_ranking_to_registry_results(monkeypatch):
    def mock_get_json(url, timeout=10):
        return {
            "skills": [
                {"name": "popular-but-unrelated", "source": "a/b", "installs": 99999, "id": "1"},
                {
                    "name": "vercel-react-best-practices",
                    "source": "vercel-labs/agent-skills",
                    "installs": 5000,
                    "id": "2",
                },
                {"name": "react", "source": "x/y", "installs": 10, "id": "3"},
            ]
        }

    monkeypatch.setattr(catalog, "load_config", lambda: {})
    monkeypatch.setattr(catalog, "get_json", mock_get_json)
    assert [r["name"] for r in catalog.search("react")] == ["react", "vercel-react-best-practices"]
    assert len(catalog.search("react", min_relevance=0)) == 3
