"""원본(추출 결과) 검색 색인 — BM25.

추출 결과(.kb/extract/*.md)를 매니페스트 항목에 연결해 위치 표지 단위 조각으로
나누고 BM25 로 검색한다. wiki 노트 검색(WikiIndexer)과 합쳐 scope 검색을
만드는 재료다. 같은 추출본을 여러 원본 파일이 공유하면 각 파일 기준으로
따로 색인한다(근거 위치가 파일마다 다르다).

질의 토큰이 하나도 없는 조각은 표를 주지 않는다 — indexer 의 0.2.4 규칙과
같은 이유(조사 때문에 토큰이 안 겹치는 한국어 질의에서 아무 조각이나 상위를
차지하는 일을 막는다). 2-gram 덕에 "기준" 질의가 "기준을" 조각과 겹친다.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
from rank_bm25 import BM25Okapi

from wiki_mcp.indexer import tokenize
from wiki_mcp.kb.chunks import split_chunks
from wiki_mcp.kb.store import KnowledgeBase, rel_key

SNIPPET_CHARS = 400


class SourceIndex:
    """추출 결과 조각 색인. build() 는 스캔 없이 기존 추출 결과만 읽는다."""

    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
        self.chunks: list[dict] = []
        self.bm25: BM25Okapi | None = None

    def build(self) -> dict:
        sources = {s.get("id"): s for s in self.kb.load_sources()}
        manifest = self.kb.load_manifest()
        self.chunks = []
        for entry in manifest.get("entries", []):
            if entry.get("status") != "ok" or not entry.get("sha256"):
                continue
            sid = entry.get("source_id", "")
            rel = entry.get("rel_path", "")
            src = sources.get(sid)
            if src is None:
                continue
            extract_file = self.kb.extract_path(entry["sha256"])
            if not extract_file.exists():
                continue
            try:
                post = frontmatter.load(extract_file)
            except Exception:
                continue
            origin = str(Path(str(src.get("path", ""))) / Path(*rel.split("/")))
            title = entry.get("title") or post.get("title", "") or Path(rel).stem
            for ch in split_chunks(post.content):
                self.chunks.append(
                    {
                        "kind": "source",
                        "source_id": sid,
                        "rel_path": rel,
                        "location": ch["location"],
                        "heading": ch["heading"],
                        "title": title,
                        "path": origin,
                        "text": ch["text"],
                    }
                )

        tokenized = [
            tokenize(f"{c['title']} {c['heading'] or ''} {c['text']}") for c in self.chunks
        ]
        self.bm25 = BM25Okapi(tokenized) if any(tokenized) else None
        return {"chunks": len(self.chunks)}

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """원본 조각 검색. 결과마다 근거 위치(rel_path·location·path)를 담는다."""
        if not self.bm25:
            return []
        tokens = tokenize(query)
        q_tokens = set(tokens)
        try:
            scores = self.bm25.get_scores(tokens)
        except Exception:
            return []
        ranked = sorted(
            (
                (i, s)
                for i, s in enumerate(scores)
                if not q_tokens.isdisjoint(self.bm25.doc_freqs[i])
            ),
            key=lambda x: -x[1],
        )[: max(1, top_k)]
        results: list[dict] = []
        for i, score in ranked:
            c = self.chunks[i]
            results.append(
                {
                    **{k: v for k, v in c.items() if k != "text"},
                    "snippet": c["text"][:SNIPPET_CHARS],
                    "score": float(score),
                }
            )
        return results

    # ─── read_extract 지원 ───────────────────────────────────
    def find_entry(self, source_id: str, rel_path: str) -> dict | None:
        """(source_id, rel_path) 로 manifest 항목을 찾는다(경로 이탈 금지 게이트)."""
        key = (source_id, rel_key(rel_path))
        for e in self.kb.load_manifest().get("entries", []):
            if (e.get("source_id"), rel_key(e.get("rel_path", ""))) == key:
                return e
        return None

    def read_extract(
        self, source_id: str, rel_path: str, offset: int = 0, limit: int = 8000
    ) -> dict:
        """추출 결과를 조각 단위(offset/limit)로 돌려준다. manifest 등록 항목만."""
        from wiki_mcp.kb.errors import KBError

        entry = self.find_entry(source_id, rel_path)
        if entry is None:
            raise KBError(
                "not_in_manifest",
                f"manifest에 없는 항목이다: {source_id} {rel_path}",
            )
        extract_file = self.kb.extract_path(entry["sha256"])
        if not extract_file.exists():
            raise KBError(
                "extract_missing",
                f"추출 파일이 없다(다시 스캔해 보라): {extract_file.name}",
            )
        content = frontmatter.load(extract_file).content
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 50000))
        piece = content[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(content) else None
        return {
            "source_id": source_id,
            "rel_path": entry.get("rel_path", rel_path),
            "path": self._origin_path(entry),
            "offset": offset,
            "limit": limit,
            "length": len(content),
            "next_offset": next_offset,
            "content": piece,
        }

    def _origin_path(self, entry: dict) -> str | None:
        for s in self.kb.load_sources():
            if s.get("id") == entry.get("source_id"):
                rel = entry.get("rel_path", "")
                return str(Path(str(s.get("path", ""))) / Path(*rel.split("/")))
        return None
