"""HWPX 추출 테스트 — 표본 HWPX를 테스트 안에서 zip으로 만들어 검증한다.

실제 한글(HWPML) 문서와 같은 구조(Contents/section*.xml, hp: 네임스페이스)를
직접 조립하므로 외부 표본 파일이 필요 없다.
"""

from __future__ import annotations

import pytest
from hwpx_helper import br, cell, make_hwpx, p, row, sec, t, tab, tbl

from wiki_mcp.extract import ExtractionError, extract_text


# ─── 문단·인라인 요소 ────────────────────────────────────────────────
class TestHwpxParagraphs:
    def test_paragraphs_each_end_with_newline(self, tmp_path):
        f = make_hwpx(
            tmp_path / "a.hwpx",
            {
                "Contents/section0.xml": sec(
                    p(t("첫 번째 문단의 내용이 충분히 있습니다"))
                    + p(t("둘째 문단 내용도 있습니다"))
                ),
            },
        )
        out = extract_text(f)
        assert out.splitlines() == [
            "첫 번째 문단의 내용이 충분히 있습니다",
            "둘째 문단 내용도 있습니다",
        ]

    def test_tab_and_line_break(self, tmp_path):
        f = make_hwpx(
            tmp_path / "a.hwpx",
            {
                "Contents/section0.xml": sec(
                    p(t("이름"), tab(), t("연락처와 소속 부서를 적는다"))
                    + p(t("줄1의 내용이 여기 있고"), br(), t("줄2의 내용이 이어진다"))
                ),
            },
        )
        out = extract_text(f)
        lines = out.splitlines()
        assert lines[0] == "이름\t연락처와 소속 부서를 적는다"
        assert lines[1] == "줄1의 내용이 여기 있고" and lines[2] == "줄2의 내용이 이어진다"

    def test_empty_text_raises_empty_text_code(self, tmp_path):
        f = make_hwpx(
            tmp_path / "a.hwpx",
            {
                "Contents/section0.xml": sec(p(t("짧"))),
            },
        )
        with pytest.raises(ExtractionError) as ei:
            extract_text(f)
        assert ei.value.code == "empty_text"


# ─── 표 ───────────────────────────────────────────────────────────────
class TestHwpxTable:
    def test_table_rows_join_cells_with_pipe(self, tmp_path):
        body = p(t("아래는 예산 표입니다. 충분히 긴 설명 문단을 둔다.")) + tbl(
            row(cell(p(t("항목"))), cell(p(t("금액")))),
            row(cell(p(t("교재"))), cell(p(t("300000")))),
        )
        f = make_hwpx(tmp_path / "a.hwpx", {"Contents/section0.xml": sec(body)})
        out = extract_text(f)
        lines = out.splitlines()
        assert "항목 | 금액" in lines
        assert "교재 | 300000" in lines

    def test_multiline_cell_flattened_into_one_line(self, tmp_path):
        body = p(t("셀에 줄이 여러 개 든 표를 검증한다. 설명 문단.")) + tbl(
            row(cell(p(t("위")), p(t("아래"))), cell(p(t("값"))))
        )
        f = make_hwpx(tmp_path / "a.hwpx", {"Contents/section0.xml": sec(body)})
        out = extract_text(f)
        assert "위 아래 | 값" in out.splitlines()


# ─── 섹션 순서·위치 표지 ──────────────────────────────────────────────
class TestHwpxSections:
    def test_sections_sorted_numerically(self, tmp_path):
        # 이름순 정렬이면 section10이 section2 앞에 온다 — 숫자순이어야 한다
        f = make_hwpx(
            tmp_path / "a.hwpx",
            {
                "Contents/section0.xml": sec(p(t("영 문단 내용이 이만큼은 있어야 한다"))),
                "Contents/section1.xml": sec(p(t("일 문단 내용이 이만큼은 있어야 한다"))),
                "Contents/section2.xml": sec(p(t("이 문단 내용이 이만큼은 있어야 한다"))),
                "Contents/section10.xml": sec(p(t("십 문단 내용이 이만큼은 있어야 한다"))),
            },
        )
        out = extract_text(f)
        order = [ln for ln in out.splitlines() if ln]
        assert order == [
            "영 문단 내용이 이만큼은 있어야 한다",
            "일 문단 내용이 이만큼은 있어야 한다",
            "이 문단 내용이 이만큼은 있어야 한다",
            "십 문단 내용이 이만큼은 있어야 한다",
        ]

    def test_section_markers_when_enabled(self, tmp_path):
        f = make_hwpx(
            tmp_path / "a.hwpx",
            {
                "Contents/section0.xml": sec(p(t("첫 섹션 본문이 이만큼은 있어야 한다"))),
                "Contents/section2.xml": sec(p(t("셋째 섹션 본문이 이만큼은 있어야 한다"))),
            },
        )
        out = extract_text(f, markers=True)
        assert "<!-- section 0 -->" in out
        assert "<!-- section 2 -->" in out
        assert out.index("<!-- section 0 -->") < out.index("<!-- section 2 -->")

    def test_no_markers_by_default(self, tmp_path):
        f = make_hwpx(
            tmp_path / "a.hwpx",
            {
                "Contents/section0.xml": sec(p(t("표지 없는 기본 동작 유지 확인용 본문"))),
            },
        )
        assert "<!-- section" not in extract_text(f)

    def test_non_section_entries_ignored(self, tmp_path):
        f = make_hwpx(
            tmp_path / "a.hwpx",
            {
                "Contents/header.xml": sec(p(t("머리말은 본문이 아니므로 제외되어야 한다"))),
                "Contents/section0.xml": sec(p(t("본문 문단만 추출된다 충분한 길이로 한다"))),
            },
        )
        out = extract_text(f)
        assert "머리말" not in out
        assert "본문 문단만" in out


# ─── 안전 상한 ────────────────────────────────────────────────────────
class TestHwpxLimits:
    def test_too_many_entries_rejected(self, tmp_path, monkeypatch):
        from wiki_mcp import extract as ex

        monkeypatch.setattr(ex, "_HWPX_MAX_ENTRIES", 3)
        sections = {f"Contents/section{i}.xml": sec(p(t(f"{i} 번째 문단 내용"))) for i in range(4)}
        f = make_hwpx(tmp_path / "a.hwpx", sections)
        with pytest.raises(ExtractionError) as ei:
            extract_text(f)
        assert ei.value.code == "corrupt"

    def test_uncompressed_total_rejected(self, tmp_path, monkeypatch):
        from wiki_mcp import extract as ex

        monkeypatch.setattr(ex, "_HWPX_MAX_UNCOMPRESSED", 100)
        f = make_hwpx(
            tmp_path / "a.hwpx",
            {
                "Contents/section0.xml": sec(p(t("가" * 200))),
            },
        )
        with pytest.raises(ExtractionError) as ei:
            extract_text(f)
        assert ei.value.code == "corrupt"

    def test_bad_zip_is_corrupt(self, tmp_path):
        f = tmp_path / "bad.hwpx"
        f.write_bytes("이것은 zip이 아닙니다".encode())
        with pytest.raises(ExtractionError) as ei:
            extract_text(f)
        assert ei.value.code == "corrupt"

    def test_no_sections_is_empty_text(self, tmp_path):
        f = make_hwpx(tmp_path / "a.hwpx", {"Preface.xml": "<preface/>"})
        with pytest.raises(ExtractionError) as ei:
            extract_text(f)
        assert ei.value.code == "empty_text"


# ─── 다른 형식의 위치 표지 (2-4) ───────────────────────────────────────
@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("pptx") is None,
    reason="python-pptx 미설치",
)
class TestPptxMarkers:
    def test_slide_markers_when_enabled(self, tmp_path):
        from pptx import Presentation

        prs = Presentation()
        s1 = prs.slides.add_slide(prs.slide_layouts[1])
        s1.shapes.title.text = "첫 슬라이드"
        s1.placeholders[1].text = "본문 내용이 여기에 충분히 들어갑니다"
        s2 = prs.slides.add_slide(prs.slide_layouts[1])
        s2.shapes.title.text = "둘째 슬라이드"
        s2.placeholders[1].text = "둘째 본문 내용도 충분히 들어갑니다"
        f = tmp_path / "a.pptx"
        prs.save(f)

        out = extract_text(f, markers=True)
        assert "<!-- slide 1 -->" in out
        assert "<!-- slide 2 -->" in out
        assert "<!-- slide" not in extract_text(f)  # 기본은 기존 동작 유지


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("openpyxl") is None,
    reason="openpyxl 미설치",
)
class TestXlsxMarkers:
    def test_sheet_markers_when_enabled(self, tmp_path):
        from openpyxl import Workbook

        wb = Workbook()
        ws1 = wb.active
        ws1.title = "예산"
        ws1.append(["항목", "금액"])
        ws2 = wb.create_sheet("인원")
        ws2.append(["이름", "역할"])
        f = tmp_path / "a.xlsx"
        wb.save(f)

        out = extract_text(f, markers=True)
        assert "<!-- sheet 예산 -->" in out
        assert "<!-- sheet 인원 -->" in out


class TestPdfPageCollection:
    """페이지 번호 유지 규칙 — 빈 페이지는 건너뛰되 번호는 그대로(위치 표지 계약)."""

    class _Page:
        def __init__(self, text: str):
            self._text = text

        def extract_text(self) -> str:
            if self._text == "boom":
                raise RuntimeError("page read error")
            return self._text

    class _Reader:
        def __init__(self, *texts: str):
            self.pages = [TestPdfPageCollection._Page(t) for t in texts]

    def test_empty_pages_skipped_numbers_kept(self):
        from wiki_mcp.extract import _pdf_collect_pages

        pages = _pdf_collect_pages(self._Reader("", "  ", "셋째 페이지 본문", "", "다섯째 본문"))
        assert pages == [(3, "셋째 페이지 본문"), (5, "다섯째 본문")]

    def test_failing_page_treated_as_empty(self):
        from wiki_mcp.extract import _pdf_collect_pages

        pages = _pdf_collect_pages(self._Reader("boom", "둘째 페이지 본문"))
        assert pages == [(2, "둘째 페이지 본문")]
