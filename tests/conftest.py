"""공용 pytest 픽스처 — LLM·네트워크·ChromaDB 없이 writer/ingest를 검증.

FakeIndex는 실제 indexer의 계약(notes/all_ids/in_links/wiki_path/build/search_hybrid)만
디스크 rescan으로 충실히 재현한다. 임베딩·BM25 랭킹은 테스트 범위 밖이므로
search_hybrid는 결정적(빈 결과 또는 주입) 스텁으로 둔다.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from wiki_mcp.graph import WikiGraph
from wiki_mcp.indexer import extract_links


class FakeIndex:
    """디스크를 rescan해 notes/all_ids/in_links를 채우는 경량 인덱스.

    create_note/update_note/add_link/delete_note가 쓰기 후 build()를 호출하므로,
    build()가 실제 인덱서처럼 wiki_path를 다시 훑도록 하여 create→add_link→delete
    흐름이 실제와 동일하게 동작한다.
    """

    def __init__(self, wiki_path: Path):
        self.wiki_path = Path(wiki_path)
        self.embedding_provider = "none"
        self.notes: dict[str, dict] = {}
        self.all_ids: set[str] = set()
        self.in_links: dict[str, set[str]] = {}
        self.out_links: dict[str, set[str]] = {}
        self.search_results: list[dict] = []  # search_hybrid 주입용
        self.build()

    def build(self, force: bool = False, verbose: bool = False) -> dict:
        self.notes.clear()
        self.all_ids.clear()
        self.in_links.clear()
        self.out_links.clear()
        for md in self.wiki_path.rglob("*.md"):
            if any(part.startswith(".") for part in md.relative_to(self.wiki_path).parts):
                continue
            try:
                post = frontmatter.load(md)
            except Exception:
                continue
            note_id = post.get("id")
            if not note_id:
                continue
            content = post.content
            links = extract_links(content)
            self.notes[note_id] = {
                "id": note_id,
                "title": post.get("title", "") or "",
                "tags": post.get("tags", []) or [],
                "source_url": post.get("source_url", "") or "",
                "path": str(md.relative_to(self.wiki_path)),
                "content": content,
                "out_links": links,
            }
            self.all_ids.add(note_id)
            for target in links:
                self.out_links.setdefault(note_id, set()).add(target)
                self.in_links.setdefault(target, set()).add(note_id)
        return {"notes": len(self.notes)}

    def search_hybrid(self, query: str, top_k: int = 5) -> list[dict]:
        return self.search_results[:top_k]


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    """빈 wiki 루트 디렉토리."""
    w = tmp_path / "wiki"
    w.mkdir()
    return w


# ─── KB 모드 픽스처 (0.3.0) ──────────────────────────────────────────
@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    """KB 루트 폴더(초기화 전)."""
    return tmp_path / "kb"


@pytest.fixture
def kb(kb_root: Path):
    """초기화된 KnowledgeBase."""
    from wiki_mcp.kb.store import KnowledgeBase

    store = KnowledgeBase(kb_root)
    store.init()
    return store


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """원본 폴더로 쓸 빈 디렉토리."""
    d = tmp_path / "docs"
    d.mkdir()
    return d


@pytest.fixture
def idx(wiki: Path) -> FakeIndex:
    return FakeIndex(wiki)


@pytest.fixture
def graph(idx: FakeIndex) -> WikiGraph:
    """실제 WikiGraph — indexer 계약만 FakeIndex로 대체."""
    return WikiGraph(idx)


def echo_note(note: dict):
    """주어진 노트 dict를 그대로 JSON으로 반환하는 complete_fn 팩토리.

    ingest_text 등에 주입해 LLM 없이 결정적으로 검증한다.
    """
    import json

    def _fn(system: str, user: str) -> str:
        return json.dumps(note, ensure_ascii=False)

    return _fn
