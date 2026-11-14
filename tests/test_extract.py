"""extract.py characterization 테스트 — 확장자별 추출·OCR 게이트·잘라내기 계약.

문서 추출 라이브러리(openpyxl/pptx/bs4 등)가 설치돼 있으면 실제 추출을 검증하고,
없으면 해당 테스트는 skip한다(오프라인 번들에는 모두 포함됨).
"""

from __future__ import annotations

import importlib.util

import pytest

from wiki_mcp.extract import ExtractionError, extract_text


def _have(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


# ─── 텍스트/미지원 확장자 passthrough ────────────────────────────────
class TestTextPassthrough:
    def test_md_read_as_is(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("# 제목\n\n본문 내용입니다.", encoding="utf-8")
        assert "본문 내용입니다." in extract_text(f)

    def test_unknown_ext_read_as_text(self, tmp_path):
        f = tmp_path / "a.log"
        f.write_text("로그 라인이 여기에 충분히 길게 있습니다.", encoding="utf-8")
        assert "로그 라인" in extract_text(f)

    def test_truncation_appends_notice(self, tmp_path):
        f = tmp_path / "big.md"
        f.write_text("가" * 500, encoding="utf-8")
        out = extract_text(f, max_chars=100)
        assert "일부만 사용됨" in out
        assert len(out) < 500


# ─── 이미지 OCR 게이트 ───────────────────────────────────────────────
class TestImageOcrGate:
    def test_image_without_ocr_errors(self, tmp_path):
        f = tmp_path / "scan.png"
        f.write_bytes(b"\x89PNG\r\n")  # 내용 무관 — ocr=False면 즉시 거부
        with pytest.raises(ExtractionError, match="OCR 필요"):
            extract_text(f, ocr=False)

    def test_image_with_ocr_but_no_binary_errors(self, tmp_path):
        f = tmp_path / "scan.png"
        f.write_bytes(b"\x89PNG\r\n")
        with pytest.raises(ExtractionError, match="OCR 바이너리 없음"):
            extract_text(f, ocr=True, ocr_bin=tmp_path / "nonexistent")


# ─── HTML 추출 ───────────────────────────────────────────────────────
@pytest.mark.skipif(not _have("bs4"), reason="beautifulsoup4 미설치")
class TestHtmlExtract:
    def test_headings_and_paragraphs(self, tmp_path):
        f = tmp_path / "a.html"
        f.write_text(
            "<html><head><title>t</title></head><body>"
            "<h1>문서 제목</h1>"
            "<p>이것은 충분히 긴 문단 텍스트입니다. 추출 하한선을 넘겨야 합니다.</p>"
            "<script>ignore()</script></body></html>",
            encoding="utf-8",
        )
        out = extract_text(f)
        assert "# 문서 제목" in out
        assert "충분히 긴 문단 텍스트입니다." in out
        assert "ignore" not in out  # script 제거

    def test_list_items(self, tmp_path):
        f = tmp_path / "a.html"
        f.write_text(
            "<body><ul>"
            "<li>첫째 항목은 충분히 깁니다</li>"
            "<li>둘째 항목도 충분히 깁니다</li>"
            "</ul></body>",
            encoding="utf-8",
        )
        out = extract_text(f)
        assert "- 첫째 항목은 충분히 깁니다" in out


# ─── XLSX 추출 ───────────────────────────────────────────────────────
@pytest.mark.skipif(not _have("openpyxl"), reason="openpyxl 미설치")
class TestXlsxExtract:
    def test_markdown_table(self, tmp_path):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "시트1"
        ws.append(["이름", "점수"])
        ws.append(["철수", 90])
        ws.append(["영희", 85])
        f = tmp_path / "a.xlsx"
        wb.save(f)
        out = extract_text(f)
        assert "## 시트1" in out
        assert "| 이름 | 점수 |" in out
        assert "철수" in out

    def test_max_rows_truncation(self, tmp_path):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        for i in range(10):
            ws.append([f"행{i}", i])
        f = tmp_path / "a.xlsx"
        wb.save(f)
        out = extract_text(f, xlsx_max_rows=3)
        assert "행만 표시" in out


# ─── PPTX 추출 ───────────────────────────────────────────────────────
@pytest.mark.skipif(not _have("pptx"), reason="python-pptx 미설치")
class TestPptxExtract:
    def test_slide_titles_and_body(self, tmp_path):
        from pptx import Presentation

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = "첫 번째 슬라이드 제목"
        slide.placeholders[1].text = "본문 내용이 충분히 길게 들어갑니다"
        f = tmp_path / "a.pptx"
        prs.save(f)
        out = extract_text(f)
        assert "## 첫 번째 슬라이드 제목" in out
        assert "본문 내용이 충분히 길게 들어갑니다" in out
