"""KB 검색 — 조각 나누기·원본 검색·read_extract 계약 (2-5)."""

from __future__ import annotations

import pytest
from hwpx_helper import make_hwpx, p, sec, t
from kb_helper import future_now

from wiki_mcp.kb.chunks import split_chunks
from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.scanner import scan_sources
from wiki_mcp.kb.search import SourceIndex
from wiki_mcp.kb.sources import add_source


# ─── 조각 나누기 ─────────────────────────────────────────────────────
class TestSplitChunks:
    def test_marker_starts_new_chunk_with_location(self):
        text = "<!-- page 1 -->\n첫 페이지 내용\n<!-- page 2 -->\n둘째 페이지 내용"
        chunks = split_chunks(text)
        assert [c["location"] for c in chunks] == ["page 1", "page 2"]
        assert "첫 페이지 내용" in chunks[0]["text"]

    def test_heading_starts_new_chunk_with_heading(self):
        text = "# 첫 제목\n본문 A\n## 둘째 제목\n본문 B"
        chunks = split_chunks(text)
        assert len(chunks) == 2
        assert chunks[1]["heading"] == "둘째 제목"
        assert "본문 B" in chunks[1]["text"]

    def test_marker_and_heading_carry_location_forward(self):
        text = "<!-- section 0 -->\n# 개요\n내용\n계속 내용"
        chunks = split_chunks(text)
        assert all(c["location"] == "section 0" for c in chunks)
        assert chunks[0]["heading"] == "개요"

    def test_long_text_split_within_max_chars(self):
        para = "가" * 400 + "\n\n"
        text = para * 10  # 약 4,020자
        chunks = split_chunks(text, max_chars=1500)
        assert len(chunks) >= 3
        assert all(len(c["text"]) <= 1500 for c in chunks)

    def test_oversized_single_line_hard_sliced(self):
        text = "나" * 4000
        chunks = split_chunks(text, max_chars=1500)
        assert [len(c["text"]) for c in chunks] == [1500, 1500, 1000]

    def test_empty_text_no_chunks(self):
        assert split_chunks("") == []
        assert split_chunks("<!-- page 1 -->\n\n") == []


# ─── 원본 검색 ───────────────────────────────────────────────────────
@pytest.fixture
def indexed(kb, source_dir):
    add_source(kb, source_dir)
    make_hwpx(
        source_dir / "정산기준.hwpx",
        {
            "Contents/section0.xml": sec(p(t("출장비 정산 기준을 담은 보고서 본문입니다"))),
            "Contents/section1.xml": sec(p(t("숙박비 한도에 관한 두 번째 섹션 본문입니다"))),
        },
    )
    (source_dir / "예산.md").write_text(
        "# 예산 메모\n\n이번 분기 예산 배분에 대한 회의 메모다.", encoding="utf-8"
    )
    scan_sources(kb, now=future_now())
    idx = SourceIndex(kb)
    idx.build()
    sid = kb.load_sources()[0]["id"]
    return idx, source_dir, sid


class TestSourceIndex:
    def test_search_returns_evidence_fields(self, indexed):
        idx, source_dir, _ = indexed
        results = idx.search("정산 기준", top_k=5)
        assert results, "검색 결과가 있어야 한다"
        top = results[0]
        assert top["kind"] == "source"
        assert top["source_id"].startswith("src-")
        assert top["rel_path"] == "정산기준.hwpx"
        assert top["location"] == "section 0"
        assert top["path"] == str(source_dir / "정산기준.hwpx")
        assert top["snippet"]
        assert top["score"] > 0

    def test_korean_stem_matches_inflected_doc(self, indexed):
        """2-gram — "기준" 질의가 "기준을"이 든 원본 문서를 찾는다."""
        idx, _, _ = indexed
        results = idx.search("기준")
        rels = [r["rel_path"] for r in results]
        assert "정산기준.hwpx" in rels

    def test_location_scoped_result(self, indexed):
        idx, _, _ = indexed
        results = idx.search("숙박비 한도")
        assert results
        assert results[0]["location"] == "section 1"

    def test_no_token_overlap_returns_empty(self, indexed):
        """질의 토큰과 하나도 겹치지 않으면 아무 조각도 돌아오지 않는다."""
        idx, _, _ = indexed
        assert idx.search("파티션 나누기") == []

    def test_shared_extract_searchable_from_both_files(self, kb, source_dir):
        """같은 내용 파일 — 각 파일 기준으로 따로 색인된다."""
        add_source(kb, source_dir)
        body = p(t("공유 추출 내용 — 동일한 본문을 가진 문서입니다"))
        make_hwpx(source_dir / "one.hwpx", {"Contents/section0.xml": sec(body)})
        make_hwpx(source_dir / "two.hwpx", {"Contents/section0.xml": sec(body)})
        scan_sources(kb, now=future_now())
        idx = SourceIndex(kb)
        idx.build()
        rels = [r["rel_path"] for r in idx.search("공유 추출")]
        assert sorted(rels) == ["one.hwpx", "two.hwpx"]

    def test_build_without_any_extract(self, kb):
        idx = SourceIndex(kb)
        out = idx.build()
        assert out == {"chunks": 0}
        assert idx.search("아무거나") == []


# ─── read_extract ─────────────────────────────────────────────────────
class TestReadExtract:
    def test_pagination_with_next_offset(self, indexed):
        idx, source_dir, sid = indexed
        first = idx.read_extract(sid, "정산기준.hwpx", limit=50)
        assert first["offset"] == 0
        assert first["next_offset"] == 50
        assert len(first["content"]) == 50
        second = idx.read_extract(sid, "정산기준.hwpx", offset=50, limit=50)
        assert second["offset"] == 50
        assert first["content"] + second["content"] == self._full(idx, sid, "정산기준.hwpx")[:100]

    @staticmethod
    def _full(idx, sid, rel):
        last = None
        offset = 0
        parts = []
        while True:
            last = idx.read_extract(sid, rel, offset=offset, limit=10000)
            parts.append(last["content"])
            if last["next_offset"] is None:
                break
            offset = last["next_offset"]
        return "".join(parts)

    def test_end_has_no_next_offset(self, indexed):
        idx, _, sid = indexed
        out = idx.read_extract(sid, "정산기준.hwpx", offset=0, limit=100000)
        assert out["next_offset"] is None

    def test_unknown_rel_path_rejected(self, indexed):
        idx, _, sid = indexed
        with pytest.raises(KBError) as ei:
            idx.read_extract(sid, "../없는파일.md")
        assert ei.value.code == "not_in_manifest"

    def test_unknown_source_rejected(self, indexed):
        idx, _, _ = indexed
        with pytest.raises(KBError) as ei:
            idx.read_extract("src-nope", "정산기준.hwpx")
        assert ei.value.code == "not_in_manifest"
