"""autoindex — mcpo 쪽 wiki-mcp 가 노트 파일 변경을 감지해 스스로 재인덱싱한다.

watch 가 만든 노트가 mcpo 재기동 전까지 dense 검색에 잡히지 않던 회귀를 막는다:
저장소에 쓰는 프로세스가 검색하는 프로세스와 같으면 새 벡터가 바로 보인다.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import pytest
from watchdog.events import DirModifiedEvent, FileModifiedEvent, FileMovedEvent

from wiki_mcp import autoindex
from wiki_mcp.indexer import WikiIndexer

requires_chroma = pytest.mark.skipif(
    importlib.util.find_spec("chromadb") is None,
    reason="chromadb 미설치 — [embed] extra 필요",
)


def _note(wiki: Path, nid: str, body: str) -> Path:
    p = wiki / f"{nid}.md"
    p.write_text(f"---\nid: {nid}\ntitle: {nid}\n---\n{body}\n", encoding="utf-8")
    return p


def _embed(text: str) -> list[float]:
    # 결정적 스텁: '출장' 이 들어간 글과 아닌 글이 다른 방향을 가리킨다
    return [1.0, 0.0, 0.1] if "출장" in text else [0.0, 1.0, 0.1]


class CountingIndex:
    def __init__(self, wiki: Path):
        self.wiki_path = wiki
        self.builds = 0
        self.done = threading.Event()

    def build(self, force: bool = False, verbose: bool = True) -> dict:
        self.builds += 1
        self.done.set()
        return {}


class TestDebounce:
    def test_burst_rebuilds_once_after_quiet(self, wiki):
        ix = CountingIndex(wiki)
        h = autoindex.DebouncedReindex(ix, delay=0.2)
        for i in range(5):
            h.dispatch(FileModifiedEvent(str(wiki / f"n{i}.md")))
            time.sleep(0.02)
        assert ix.done.wait(2)
        time.sleep(0.4)
        assert ix.builds == 1

    @pytest.mark.parametrize(
        "make",
        [
            lambda w: FileModifiedEvent(str(w / "note.txt")),
            lambda w: FileModifiedEvent(str(w / ".templates" / "concept.md")),
            lambda w: DirModifiedEvent(str(w / "sub")),
        ],
    )
    def test_ignores_non_notes(self, wiki, make):
        ix = CountingIndex(wiki)
        autoindex.DebouncedReindex(ix, delay=0.05).dispatch(make(wiki))
        assert not ix.done.wait(0.3)

    def test_atomic_rename_into_md_counts(self, wiki):
        # 편집기(Obsidian 등)는 임시 파일을 쓰고 .md 로 rename 한다
        ix = CountingIndex(wiki)
        autoindex.DebouncedReindex(ix, delay=0.05).dispatch(
            FileMovedEvent(str(wiki / ".note.md.tmp"), str(wiki / "note.md"))
        )
        assert ix.done.wait(1)


def _wait(pred, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


@requires_chroma
class TestLiveStore:
    @pytest.fixture
    def ix(self, wiki, tmp_path, monkeypatch) -> WikiIndexer:
        _note(wiki, "monitoring", "서비스 모니터링 절차")
        indexer = WikiIndexer(wiki, tmp_path / "data")
        indexer.embedding_disabled = False
        monkeypatch.setattr(indexer, "_embed", _embed)
        indexer.build(verbose=False)
        return indexer

    def test_note_written_elsewhere_reaches_dense_search(self, ix, wiki):
        observer = autoindex.start(ix, delay=0.2)
        try:
            # watch 프로세스가 노트를 쓴 것과 같다 — 이 인덱서는 파일 시스템으로만 안다
            _note(wiki, "travel-a", "출장비 정산 기한")
            time.sleep(0.05)
            _note(wiki, "travel-b", "출장 숙박비 한도")
            assert _wait(lambda: ix.collection.count() == 3)
        finally:
            observer.stop()
            observer.join(5)

        dense = ix.collection.query(query_embeddings=[_embed("출장")], n_results=2)["ids"][0]
        assert set(dense) == {"travel-a", "travel-b"}
        # 두 새 노트의 벡터가 같아 dense 순위끼리는 동률 — 검색 경로에 들어왔는지만 본다
        assert ix.search_hybrid("출장", top_k=1)[0]["id"] in {"travel-a", "travel-b"}

    def test_deleted_note_leaves_store(self, ix, wiki):
        _note(wiki, "travel-a", "출장비 정산 기한")
        ix.build(verbose=False)
        observer = autoindex.start(ix, delay=0.2)
        try:
            (wiki / "travel-a.md").unlink()
            assert _wait(lambda: ix.collection.count() == 1)
        finally:
            observer.stop()
            observer.join(5)
        assert "travel-a" not in ix.notes
