"""원본(추출 결과) 검색 색인 — BM25.

추출 결과(.kb/extract/*.md)를 매니페스트 항목에 연결해 위치 표지 단위 조각으로
나누고 BM25 로 검색한다. wiki 노트 검색(WikiIndexer)과 합쳐 scope 검색을
만드는 재료다. 같은 추출본을 여러 원본 파일이 공유하면 각 파일 기준으로
따로 색인한다(근거 위치가 파일마다 다르다).

색인 최신성(H4): search·read_extract 전에 manifest.json·sources.yaml 의
(mtime_ns, size) 스탬프를 확인해 바뀌었으면 다시 만든다 — 앱이 CLI 로 스캔·
해제한 결과가 떠 있는 서버에도 바로 반영된다. 해제된 원본의 조각은 즉시
사라진다.

질의 가드(M2): 질의 원 토큰 하나라도 겹치거나, 2-gram 만 겹칠 때는 질의
2-gram 의 절반 이상이 겹칠 때만 표를 준다. 흔한 어미 2-gram("니다" 등)은
불용 목록에 둔다. 이 규칙이 없으면 조사·어미 우연 일치로 아무 조각이나
상위를 차지한다.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import frontmatter
from rank_bm25 import BM25Okapi

from wiki_mcp.indexer import _HANGUL_RUN_RE, _TOKEN_RE, tokenize
from wiki_mcp.kb.chunks import split_chunks
from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.store import KnowledgeBase, normalize_rel_path, rel_key

SNIPPET_CHARS = 400

# 흔한 어미·대화 표현 2-gram — 질의 가드에서 제외한다(M2)
_QUERY_STOP_BIGRAMS = {
    "니다",
    "합니",
    "습니",
    "임니",
    "하는",
    "있는",
    "에서",
    "으로",
    "런지",
    "주세",
    "세요",
    "알려",
    "찾아",
    "검색",
    "어떤",
    "어떻",
    "무엇",
    "대해",
    "관련",
    "좀부",
}


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _query_token_sets(query: str) -> tuple[set[str], set[str]]:
    """질의를 (원 토큰 집합, 유의미 2-gram 집합)으로 나눈다(M2)."""
    raw = _TOKEN_RE.findall(query.lower())
    original = set(raw)
    bigrams: set[str] = set()
    for tok in raw:
        for run in _HANGUL_RUN_RE.findall(tok):
            for i in range(len(run) - 1):
                bigrams.add(run[i : i + 2])
    return original, {b for b in bigrams if b not in _QUERY_STOP_BIGRAMS}


def _passes_query_guard(q_original: set[str], q_bigrams: set[str], doc_tokens: set[str]) -> bool:
    """원 토큰 하나라도 겹치면 통과. 2-gram 만 겹칠 땐 절반 이상일 때 통과."""
    if q_original & doc_tokens:
        return True
    if not q_bigrams:
        return False
    needed = (len(q_bigrams) + 1) // 2
    return len(q_bigrams & doc_tokens) >= needed


def _origin_join(root_str: str, entry: dict) -> str:
    """원본 절대 경로 조립(M10) — 파일시스템 표기(fs_rel_path)를 그대로 쓴다.

    NFD 파일명이 macOS 에서 Windows 로 옮겨진 경우 rel_path(NFC) 로는 실제
    파일을 못 찾을 수 있어, 스캔 때 남겨둔 표기를 우선한다.
    """
    fs_rel = entry.get("fs_rel_path") or entry.get("rel_path", "")
    return str(Path(root_str) / Path(*Path(fs_rel).parts))


class SourceIndex:
    """추출 결과 조각 색인. build() 는 스캔 없이 기존 추출 결과만 읽는다."""

    def __init__(self, kb: KnowledgeBase, *, max_index_chars: int | None = None):
        self.kb = kb
        self.max_index_chars = max_index_chars  # None = 제한 없음. 서버가 config 전달(H8)
        self.chunks: list[dict] = []
        self.bm25: BM25Okapi | None = None
        self.index_truncated: list[str] = []  # 색인에 못 넣은 파일 목록(H8)
        self.ready = False  # 백그라운드 빌드 완료(H5)
        self._lock = threading.RLock()
        self._stamp: tuple | None = None

    # ─── 최신성(H4) ──────────────────────────────────────────
    def _current_stamp(self) -> tuple:
        def one(p: Path) -> tuple[int, int]:
            try:
                st = p.stat()
            except OSError:
                return (0, 0)
            return (st.st_mtime_ns, st.st_size)

        return (one(self.kb.manifest_file), one(self.kb.sources_file))

    def _maybe_refresh(self) -> None:
        """manifest·sources 가 스캔/해제로 바뀌었으면 색인을 다시 만든다."""
        if self._stamp != self._current_stamp():
            self.build()

    # ─── 구축 ────────────────────────────────────────────────
    def build(self) -> dict:
        with self._lock:
            sources = {s.get("id"): s for s in self.kb.load_sources()}
            manifest = self.kb.load_manifest()
            self.chunks = []
            self.index_truncated = []
            used_chars = 0
            for entry in manifest.get("entries", []):
                if entry.get("status") != "ok" or not entry.get("sha256"):
                    continue
                sid = entry.get("source_id", "")
                rel = entry.get("rel_path", "")
                src = sources.get(sid)
                if src is None:
                    continue  # 등록이 해제된 원본 — 조각을 남기지 않는다
                extract_file = self.kb.extract_path(entry["sha256"])
                if not extract_file.exists():
                    _log(f"⚠️ 추출 파일 없음(건너뜀): {extract_file.name} ({rel})")
                    continue
                try:
                    post = frontmatter.load(extract_file)
                except Exception as e:  # L2 — 조용히 넘기지 않고 로그를 남긴다
                    _log(f"⚠️ 추출 파일 파싱 실패({type(e).__name__}): {rel}")
                    continue
                origin = _origin_join(str(src.get("path", "")), entry)
                title = entry.get("title") or post.get("title", "") or Path(rel).stem
                for ch in split_chunks(post.content):
                    if (
                        self.max_index_chars is not None
                        and used_chars + len(ch["text"]) > self.max_index_chars
                    ):
                        if rel not in self.index_truncated:
                            self.index_truncated.append(rel)
                        continue  # 이 파일의 남은 조각은 넘친 것으로 보고(H8)
                    used_chars += len(ch["text"])
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
            self._stamp = self._current_stamp()
            self.ready = True
            return {"chunks": len(self.chunks)}

    # ─── 검색 ────────────────────────────────────────────────
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """원본 조각 검색. 결과마다 근거 위치(rel_path·location·path)를 담는다."""
        with self._lock:
            if not self.ready:
                self.build()
            else:
                self._maybe_refresh()
            if not self.bm25:
                return []
            tokens = tokenize(query)
            q_original, q_bigrams = _query_token_sets(query)
            try:
                scores = self.bm25.get_scores(tokens)
            except Exception as e:  # L2
                _log(f"⚠️ 원본 검색 실패({type(e).__name__}): {e}")
                return []
            ranked = []
            for i, score in enumerate(scores):
                doc_tokens = set(self.bm25.doc_freqs[i])
                if not _passes_query_guard(q_original, q_bigrams, doc_tokens):
                    continue
                ranked.append((i, score))
            ranked.sort(key=lambda x: -x[1])
            results: list[dict] = []
            for i, score in ranked[: max(1, top_k)]:
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
        rel = normalize_rel_path(str(rel_path).replace("\\", "/"))
        key = (source_id, rel_key(rel))
        for e in self.kb.load_manifest().get("entries", []):
            if (e.get("source_id"), rel_key(e.get("rel_path", ""))) == key:
                return e
        return None

    def read_extract(
        self, source_id: str, rel_path: str, offset: int = 0, limit: int = 8000
    ) -> dict:
        """추출 결과를 조각 단위(offset/limit)로 돌려준다. manifest 등록 항목만."""
        with self._lock:
            self._maybe_refresh()
        entry = self.find_entry(source_id, rel_path)
        if entry is None:
            raise KBError(
                "not_in_manifest",
                f"manifest에 없는 항목이다: {source_id} {rel_path}",
            )
        if entry.get("status") != "ok":  # M7 — 실패 항목은 사유와 함께 거부
            raise KBError(
                "not_extracted",
                f"추출에 성공한 항목이 아니다(사유: {entry.get('reason')}) — "
                f"원본을 고친 뒤 scan_sources 로 다시 동기화한다",
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
                return _origin_join(str(s.get("path", "")), entry)
        return None
