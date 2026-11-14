"""graph.py characterization 테스트 — 링크 그래프 탐색·broken link·도메인 집계."""
from __future__ import annotations

from wiki_mcp.writer import create_note


def _seed(idx, graph, nid, domain="llm", links=None, body="본문"):
    return create_note(
        idx, graph, id=nid, title=nid.upper(), body=body, tags=[],
        domain=domain, links=links,
    )


class TestDomainOf:
    def test_domain_from_path(self, idx, graph):
        _seed(idx, graph, "rag", domain="llm")
        assert graph.domain_of("rag") == "llm"

    def test_unknown_note_empty(self, idx, graph):
        assert graph.domain_of("ghost") == ""


class TestListDomains:
    def test_counts_per_domain(self, idx, graph):
        _seed(idx, graph, "rag", domain="llm")
        _seed(idx, graph, "embedding", domain="llm")
        _seed(idx, graph, "docker", domain="infra")
        assert graph.list_domains() == {"llm": 2, "infra": 1}


class TestNeighbors:
    def test_unknown_note_empty(self, idx, graph):
        assert graph.neighbors("ghost") == {}

    def test_depth_1_includes_out_and_in(self, idx, graph):
        _seed(idx, graph, "embedding")
        _seed(idx, graph, "rag", body="[[embedding]] 참조")  # rag → embedding
        # rag 기준: out_link 로 embedding
        levels = graph.neighbors("rag", depth=1)
        assert levels[0] == ["rag"]
        assert "embedding" in levels[1]
        # embedding 기준: in_link 로 rag (역방향도 이웃)
        levels_rev = graph.neighbors("embedding", depth=1)
        assert "rag" in levels_rev[1]

    def test_depth_2_transitive(self, idx, graph):
        _seed(idx, graph, "c")
        _seed(idx, graph, "b", body="[[c]] 참조")
        _seed(idx, graph, "a", body="[[b]] 참조")
        levels = graph.neighbors("a", depth=2)
        assert "b" in levels[1]
        assert "c" in levels[2]

    def test_depth_limit_respected(self, idx, graph):
        _seed(idx, graph, "c")
        _seed(idx, graph, "b", body="[[c]] 참조")
        _seed(idx, graph, "a", body="[[b]] 참조")
        levels = graph.neighbors("a", depth=1)
        assert 2 not in levels

    def test_cycle_terminates(self, idx, graph):
        # a → b → a 순환에서 무한루프 없이 종료
        _seed(idx, graph, "b")
        _seed(idx, graph, "a", body="[[b]] 참조")
        create_note(idx, graph, id="b", title="B", body="[[a]] 참조", tags=[],
                    domain="llm", force=True)
        levels = graph.neighbors("a", depth=5)
        assert "b" in levels[1]

    def test_broken_target_not_a_neighbor(self, idx, graph):
        # 존재하지 않는 노트로의 링크는 이웃에 포함되지 않음
        _seed(idx, graph, "a", body="[[ghost]] 참조")
        levels = graph.neighbors("a", depth=1)
        assert levels.get(1, []) == []


class TestBrokenLinks:
    def test_detects_broken(self, idx, graph):
        _seed(idx, graph, "a", body="[[ghost]] 참조")
        broken = graph.find_broken_links()
        assert {"source": "a", "broken_target": "ghost"} in broken

    def test_valid_link_not_broken(self, idx, graph):
        _seed(idx, graph, "b")
        _seed(idx, graph, "a", body="[[b]] 참조")
        assert graph.find_broken_links() == []


class TestHubs:
    def test_hub_score_counts_inbound(self, idx, graph):
        _seed(idx, graph, "hub")
        _seed(idx, graph, "a", body="[[hub]] 참조")
        _seed(idx, graph, "b", body="[[hub]] 참조")
        assert graph.hub_score("hub") == 2

    def test_hub_score_zero_for_unlinked(self, idx, graph):
        _seed(idx, graph, "lonely")
        assert graph.hub_score("lonely") == 0

    def test_top_hubs_sorted(self, idx, graph):
        _seed(idx, graph, "hub")
        _seed(idx, graph, "minor")
        _seed(idx, graph, "a", body="[[hub]] 참조")
        _seed(idx, graph, "b", body="[[hub]] 그리고 [[minor]] 참조")
        hubs = graph.top_hubs(limit=2)
        assert hubs[0][0] == "hub"
        assert hubs[0][1] == 2
