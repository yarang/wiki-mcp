"""indexer.build 임베딩 캐시 — 저장소에 임베딩이 없는 노트는 캐시와 무관하게 다시 임베딩한다.

임베딩 서버가 꺼진 채 처음 인덱싱한 뒤 서버가 켜져도 Hybrid 검색이 빈 dense 인덱스로
BM25 결과만 돌려주던 회귀를 막는다. ChromaDB 는 tmp 디렉토리에 실제로 띄우고 _embed 만 스텁.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import frontmatter
import pytest

from wiki_mcp.indexer import WikiIndexer, tokenize

IDS = ("rag", "embedding")

requires_chroma = pytest.mark.skipif(
    importlib.util.find_spec("chromadb") is None,
    reason="chromadb 미설치 — [embed] extra 필요",
)


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


@requires_chroma
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
        cache = {nid: ix._content_hash(frontmatter.load(wiki / f"{nid}.md").content) for nid in IDS}
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


@requires_chroma
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
        (wiki / "rag.md").write_text(
            "---\nid: rag\ntitle: rag\n---\n검색 증강 생성\n", encoding="utf-8"
        )
        data = tmp_path / "data"
        indexer = WikiIndexer(wiki, data, store=False)
        indexer.embedding_disabled = False  # 저장소가 없으면 임베딩 설정과 무관하게 BM25 전용

        indexer.build(verbose=False)
        assert indexer.collection is None
        assert not (data / "chroma").exists()
        assert not (data / "index_cache.json").exists()
        assert [r["id"] for r in indexer.search_hybrid("검색 증강")] == ["rag"]


def _note_md(wiki: Path, nid: str, body: str) -> None:
    (wiki / f"{nid}.md").write_text(
        f"---\nid: {nid}\ntitle: {nid}\n---\n{body}\n", encoding="utf-8"
    )


class TestZeroScoreBm25:
    """BM25 점수가 0인 노트는 RRF 표를 받지 않는다.

    한국어는 조사가 붙어 질의('노트북을')와 본문('노트북')이 한 토큰도 안 겹치기 쉽다. 그때 점수 0인
    노트까지 순서대로 표를 주면 dense 1위가 아무 노트(mini01 에서는 허브 `index`)에 밀렸다.
    """

    QUERY = "회사 노트북을 밖으로 들고 나가려면 누구 허락이 필요해?"

    @requires_chroma
    def test_dense_top_wins_when_no_token_overlaps(self, wiki, tmp_path, monkeypatch):
        for i in range(8):
            _note_md(wiki, f"filler-{i}", f"서비스 모니터링 절차 {i}")
        _note_md(wiki, "removal-policy", "노트북 반출은 부서장 승인 후 가능")
        indexer = WikiIndexer(wiki, tmp_path / "data")
        indexer.embedding_disabled = False
        monkeypatch.setattr(
            indexer, "_embed", lambda t: [1.0, 0.0, 0.1] if "노트북" in t else [0.0, 1.0, 0.1]
        )
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


# ─── 한국어 2-gram (0.3.0) ────────────────────────────────────────────
class TestKoreanBigram:
    """한글 연속 구간에서 글자 2-gram을 원 토큰과 함께 추가한다.

    "기준"으로 검색하면 "기준을"이 든 노트를 못 찾던 한국어 검색 누락(knowledge-base.md 3장)을
    조사가 붙은 표기에서도 겹치게 만든다. 영어·숫자·[[id]] 처리는 그대로.
    """

    def test_bigram_added_to_hangul_token(self):
        # 예시 계약: "기준을" → "기준을", "기준", "준을"
        assert tokenize("기준을") == ["기준을", "기준", "준을"]

    def test_single_hangul_char_token_kept_without_bigram(self):
        assert tokenize("산") == ["산"]

    def test_two_char_hangul_token_has_no_extra_bigram(self):
        assert tokenize("기준") == ["기준"]

    def test_english_untouched(self):
        assert tokenize("RAG pipeline v2") == ["rag", "pipeline", "v2"]

    def test_id_link_token_untouched(self):
        assert tokenize("[[mcp-server]]") == ["[[mcp-server]]"]

    def test_mixed_tokens(self):
        toks = tokenize("기준을 RAG")
        assert "기준을" in toks and "기준" in toks and "준을" in toks and "rag" in toks

    def test_inflected_word_found_by_bare_stem(self, wiki, tmp_path):
        _note_md(wiki, "policy", "출장비 정산 기준을 회의에서 변경했다")
        indexer = WikiIndexer(wiki, tmp_path / "data", store=False)
        indexer.build(verbose=False)

        assert [r["id"] for r in indexer.search_hybrid("기준")] == ["policy"]

    def test_inflected_query_matches_bare_doc(self, wiki, tmp_path):
        _note_md(wiki, "policy", "출장비 정산 기준")
        indexer = WikiIndexer(wiki, tmp_path / "data", store=False)
        indexer.build(verbose=False)

        assert [r["id"] for r in indexer.search_hybrid("기준을")] == ["policy"]

    def test_query_token_gate_still_holds_with_bigram(self, wiki, tmp_path):
        """질의 토큰과 하나도 겹치지 않는 노트는 2-gram이 있어도 표를 받지 않는다."""
        _note_md(wiki, "rag", "검색 증강 생성")
        _note_md(wiki, "monitoring", "서비스 모니터링 절차")
        indexer = WikiIndexer(wiki, tmp_path / "data", store=False)
        indexer.build(verbose=False)

        assert indexer.search_hybrid("파티션 나누기") == []
        assert [r["id"] for r in indexer.search_hybrid("모니터링 절차")] == ["monitoring"]


# ─── chromadb 선택 의존성 (0.3.0) ────────────────────────────────────
class TestChromaOptional:
    """chromadb가 없으면 store=True 인덱서도 조용히 BM25 전용으로 동작한다."""

    def test_store_indexer_degrades_to_bm25(self, wiki, tmp_path, monkeypatch, capsys):
        _note_md(wiki, "rag", "검색 증강 생성")
        # sys.modules에 None을 심으면 import chromadb가 ImportError를 낸다
        monkeypatch.setitem(sys.modules, "chromadb", None)
        monkeypatch.setattr("wiki_mcp.indexer._CHROMA_RESOLVED", False)

        indexer = WikiIndexer(wiki, tmp_path / "data")
        indexer.build(verbose=False)

        assert indexer.collection is None
        assert not indexer.dense
        assert [r["id"] for r in indexer.search_hybrid("검색")] == ["rag"]
        err = capsys.readouterr().err
        assert "BM25 전용" in err
        assert "wiki-mcp[embed]" in err

    def test_missing_chroma_notice_printed_once(self, wiki, tmp_path, monkeypatch, capsys):
        monkeypatch.setitem(sys.modules, "chromadb", None)
        monkeypatch.setattr("wiki_mcp.indexer._CHROMA_RESOLVED", False)
        WikiIndexer(wiki, tmp_path / "a")
        WikiIndexer(wiki, tmp_path / "b")
        err = capsys.readouterr().err
        assert err.count("BM25 전용") == 1
