"""indexer.build 임베딩 캐시 — 저장소에 임베딩이 없는 노트는 캐시와 무관하게 다시 임베딩한다.

임베딩 서버가 꺼진 채 처음 인덱싱한 뒤 서버가 켜져도 Hybrid 검색이 빈 dense 인덱스로
BM25 결과만 돌려주던 회귀를 막는다. ChromaDB 는 tmp 디렉토리에 실제로 띄우고 _embed 만 스텁.
"""
from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest

from wiki_mcp.indexer import WikiIndexer, tokenize

IDS = ("rag", "embedding")


def _up(text: str) -> list[float]:
    return [0.1, 0.2, 0.3]


def _down(text: str) -> list[float]:
    raise ConnectionError("embedding server down")


@pytest.fixture
def ix(tmp_path: Path, wiki: Path) -> WikiIndexer:
    for nid in IDS:
        (wiki / f"{nid}.md").write_text(
            f"---\nid: {nid}\ntitle: {nid}\n---\n{nid} 본문\n", encoding="utf-8"
        )
    indexer = WikiIndexer(wiki, tmp_path / "data")
    indexer.embedding_disabled = False
    return indexer


class TestEmbedRetry:
    def test_failed_notes_are_retried(self, ix, monkeypatch):
        monkeypatch.setattr(ix, "_embed", _down)
        assert ix.build(verbose=False)["embedded"] == 0
        assert ix.collection.count() == 0

        monkeypatch.setattr(ix, "_embed", _up)
        assert ix.build(verbose=False)["embedded"] == 2
        assert ix.collection.count() == 2

    def test_unchanged_notes_are_not_reembedded(self, ix, monkeypatch):
        monkeypatch.setattr(ix, "_embed", _up)
        ix.build(verbose=False)
        assert ix.build(verbose=False)["embedded"] == 0

    def test_bm25_only_run_does_not_block_embedding(self, ix, monkeypatch):
        ix.embedding_disabled = True
        ix.build(verbose=False)

        ix.embedding_disabled = False
        monkeypatch.setattr(ix, "_embed", _up)
        ix.build(verbose=False)
        assert ix.collection.count() == 2

    def test_cache_without_stored_embeddings_heals(self, ix, wiki, monkeypatch):
        # 0.2.0 은 임베딩에 실패한 노트도 캐시에 적었다: 해시는 맞는데 저장소는 비어 있는 상태.
        cache = {
            nid: ix._content_hash(frontmatter.load(wiki / f"{nid}.md").content) for nid in IDS
        }
        (ix.data_dir / "index_cache.json").write_text(json.dumps(cache), encoding="utf-8")

        monkeypatch.setattr(ix, "_embed", _up)
        ix.build(verbose=False)
        assert ix.collection.count() == 2


def _up4(text: str) -> list[float]:
    return [0.1, 0.2, 0.3, 0.4]


def _reopen(ix: WikiIndexer, model: str) -> WikiIndexer:
    indexer = WikiIndexer(ix.wiki_path, ix.data_dir, embedding_model=model)
    indexer.embedding_disabled = False
    return indexer


class TestEmbeddingModelChange:
    """임베딩 모델(차원)이 바뀌면 저장소를 새로 만든다 — 옛 벡터가 남으면 dense 검색이 조용히 꺼진다."""

    def test_same_model_reopen_keeps_store(self, ix, monkeypatch):
        monkeypatch.setattr(ix, "_embed", _up)
        ix.build(verbose=False)

        ix2 = _reopen(ix, ix.embedding_model)
        monkeypatch.setattr(ix2, "_embed", _up)
        assert ix2.build(verbose=False)["embedded"] == 0
        assert ix2.collection.count() == 2

    def test_new_model_rebuilds_store(self, ix, monkeypatch):
        monkeypatch.setattr(ix, "_embed", _up)
        ix.build(verbose=False)

        ix2 = _reopen(ix, "other-model")
        monkeypatch.setattr(ix2, "_embed", _up4)
        assert ix2.build(verbose=False)["embedded"] == 2
        assert ix2.collection.count() == 2
        # search_hybrid 는 dense 실패를 삼키므로 저장소에 직접 질의해 차원을 확인한다
        res = ix2.collection.query(query_embeddings=[_up4("q")], n_results=1)
        assert res["ids"][0]

    def test_legacy_store_without_model_is_rebuilt(self, ix, monkeypatch):
        # 0.2.1 까지는 모델 이름을 적지 않았다: 행은 있는데 어떤 모델의 벡터인지 모른다.
        ix.client.delete_collection("wiki")
        legacy = ix.client.create_collection(name="wiki", metadata={"hnsw:space": "cosine"})
        legacy.upsert(ids=list(IDS), embeddings=[_up("") for _ in IDS], documents=list(IDS))

        ix2 = _reopen(ix, ix.embedding_model)
        monkeypatch.setattr(ix2, "_embed", _up4)
        assert ix2.build(verbose=False)["embedded"] == 2
        assert ix2.collection.query(query_embeddings=[_up4("q")], n_results=1)["ids"][0]

    def test_bm25_only_leaves_store_alone(self, ix, monkeypatch):
        monkeypatch.setattr(ix, "_embed", _up)
        ix.build(verbose=False)

        monkeypatch.setattr("wiki_mcp.indexer.EMBEDDING_DISABLED", True)
        WikiIndexer(ix.wiki_path, ix.data_dir, embedding_model="other-model")
        assert ix.client.get_collection("wiki").count() == 2


class TestStorelessIndexer:
    """store=False (watch 용) — 벡터 저장소와 임베딩 캐시는 mcpo 쪽 인덱서 하나만 쓴다."""

    def test_does_not_touch_store_or_cache(self, wiki, tmp_path):
        (wiki / "rag.md").write_text("---\nid: rag\ntitle: rag\n---\n검색 증강 생성\n", encoding="utf-8")
        data = tmp_path / "data"
        indexer = WikiIndexer(wiki, data, store=False)
        indexer.embedding_disabled = False  # 저장소가 없으면 임베딩 설정과 무관하게 BM25 전용

        indexer.build(verbose=False)
        assert indexer.collection is None
        assert not (data / "chroma").exists()
        assert not (data / "index_cache.json").exists()
        assert [r["id"] for r in indexer.search_hybrid("검색 증강")] == ["rag"]


def _note_md(wiki: Path, nid: str, body: str) -> None:
    (wiki / f"{nid}.md").write_text(f"---\nid: {nid}\ntitle: {nid}\n---\n{body}\n", encoding="utf-8")


class TestZeroScoreBm25:
    """BM25 점수가 0인 노트는 RRF 표를 받지 않는다.

    한국어는 조사가 붙어 질의('노트북을')와 본문('노트북')이 한 토큰도 안 겹치기 쉽다. 그때 점수 0인
    노트까지 순서대로 표를 주면 dense 1위가 아무 노트(mini01 에서는 허브 `index`)에 밀렸다.
    """

    QUERY = "회사 노트북을 밖으로 들고 나가려면 누구 허락이 필요해?"

    def test_dense_top_wins_when_no_token_overlaps(self, wiki, tmp_path, monkeypatch):
        for i in range(8):
            _note_md(wiki, f"filler-{i}", f"서비스 모니터링 절차 {i}")
        _note_md(wiki, "removal-policy", "노트북 반출은 부서장 승인 후 가능")
        indexer = WikiIndexer(wiki, tmp_path / "data")
        indexer.embedding_disabled = False
        monkeypatch.setattr(indexer, "_embed", lambda t: [1.0, 0.0, 0.1] if "노트북" in t else [0.0, 1.0, 0.1])
        indexer.build(verbose=False)
        assert not any(indexer.bm25.get_scores(tokenize(self.QUERY)))

        assert indexer.search_hybrid(self.QUERY, top_k=3)[0]["id"] == "removal-policy"

    def test_bm25_only_returns_only_matching_notes(self, wiki, tmp_path):
        _note_md(wiki, "rag", "검색 증강 생성")
        _note_md(wiki, "monitoring", "서비스 모니터링 절차")
        indexer = WikiIndexer(wiki, tmp_path / "data", store=False)
        indexer.build(verbose=False)

        assert [r["id"] for r in indexer.search_hybrid("검색")] == ["rag"]
        assert indexer.search_hybrid(self.QUERY) == []
