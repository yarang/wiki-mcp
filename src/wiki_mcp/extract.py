"""문서 텍스트 추출 — 완전 오프라인(에어갭).

open-webui가 명시 의존성으로 포함하는 순수 파이썬 라이브러리만 사용한다
(pypdf, docx2txt, python-pptx, openpyxl, beautifulsoup4). 네트워크·모델
다운로드 없음. bs4는 stdlib html.parser를 사용해 lxml에 의존하지 않는다.

지원: .pdf .docx .pptx .xlsx .html/.htm  (그 외/텍스트는 그대로 읽음)
미지원: 스캔(이미지) PDF — 텍스트가 없어 OCR이 필요하나 에어갭 부담으로 제외.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# 그대로 읽는 텍스트 확장자
TEXT_EXTS = {".txt", ".md", ".markdown", ".text"}
# OCR로 처리하는 이미지 확장자
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".heic"}

_MIN_CHARS = 20  # 유의미 텍스트 최소 길이 (미만이면 추출 실패로 간주)


class ExtractionError(Exception):
    """문서에서 유효한 텍스트를 얻지 못함 (라이브러리 없음/스캔본/파싱 실패 등)."""


def _ocr_file(path: Path, ocr_bin, timeout: int = 300) -> str:
    """vision-ocr 바이너리(Apple Vision)로 PDF/이미지에서 텍스트 인식. 오프라인."""
    if not ocr_bin or not Path(ocr_bin).exists():
        raise ExtractionError("OCR 바이너리 없음(vision-ocr) — OCR 불가")
    try:
        r = subprocess.run(
            [str(ocr_bin), str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ExtractionError("OCR 시간 초과")
    except Exception as e:
        raise ExtractionError(f"OCR 실행 실패: {e}")
    if r.returncode != 0:
        raise ExtractionError(f"OCR 실패: {(r.stderr or '').strip()[:200]}")
    return r.stdout


def _extract_pdf(path: Path, *, ocr: bool = False, ocr_bin=None) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractionError("pypdf 미설치 — PDF 추출 불가")
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise ExtractionError(f"PDF 열기 실패: {e}")
    parts: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            parts.append(t.strip())
    text = "\n\n".join(parts)
    if len(text.strip()) >= _MIN_CHARS:
        return text

    # 텍스트가 빈약함 = 스캔/이미지 PDF → OCR 폴백
    if ocr and ocr_bin and Path(ocr_bin).exists():
        ocr_text = _ocr_file(path, ocr_bin)
        if len(ocr_text.strip()) >= _MIN_CHARS:
            return ocr_text
        raise ExtractionError("스캔 PDF로 추정되나 OCR로도 텍스트를 얻지 못함")
    raise ExtractionError(
        "추출 텍스트가 거의 없음 — 스캔/이미지 PDF로 추정. "
        "OCR을 켜거나(WIKI_INGEST_OCR=1) 텍스트 PDF로 변환 후 재투입 필요."
    )


def _extract_docx(path: Path) -> str:
    try:
        import docx2txt
    except ImportError:
        raise ExtractionError("docx2txt 미설치 — docx 추출 불가")
    try:
        text = docx2txt.process(str(path)) or ""
    except Exception as e:
        raise ExtractionError(f"docx 추출 실패: {e}")
    if len(text.strip()) < _MIN_CHARS:
        raise ExtractionError("docx에서 텍스트를 얻지 못함")
    return text


def _extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        raise ExtractionError("python-pptx 미설치 — pptx 추출 불가")
    try:
        prs = Presentation(str(path))
    except Exception as e:
        raise ExtractionError(f"pptx 열기 실패: {e}")
    out: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        title = ""
        try:
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = (slide.shapes.title.text or "").strip()
        except Exception:
            title = ""
        body: list[str] = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            txt = (shape.text or "").strip()
            if txt and txt != title:
                body.append(txt)
        heading = f"## {title}" if title else f"## 슬라이드 {i}"
        out.append(heading + ("\n\n" + "\n".join(body) if body else ""))
    text = "\n\n".join(out)
    if len(text.strip()) < _MIN_CHARS:
        raise ExtractionError("pptx에서 텍스트를 얻지 못함")
    return text


def _extract_xlsx(path: Path, max_rows: int = 200) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ExtractionError("openpyxl 미설치 — xlsx 추출 불가")
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        raise ExtractionError(f"xlsx 열기 실패: {e}")
    out: list[str] = []
    for ws in wb.worksheets:
        rows: list[list[str]] = []
        truncated = False
        for ri, row in enumerate(ws.iter_rows(values_only=True)):
            if ri >= max_rows:
                truncated = True
                break
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                rows.append(cells)
        if not rows:
            continue
        width = max(len(r) for r in rows)

        def _fmt(r: list[str]) -> str:
            r = r + [""] * (width - len(r))
            return "| " + " | ".join(c.replace("|", "\\|") for c in r) + " |"

        md = [f"## {ws.title}", "", _fmt(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
        md += [_fmt(r) for r in rows[1:]]
        if truncated:
            md.append(f"\n_(상위 {max_rows}행만 표시)_")
        out.append("\n".join(md))
    wb.close()
    text = "\n\n".join(out)
    if len(text.strip()) < _MIN_CHARS:
        raise ExtractionError("xlsx에서 데이터를 얻지 못함")
    return text


def _extract_html(path: Path) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ExtractionError("beautifulsoup4 미설치 — html 추출 불가")
    raw = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "html.parser")  # stdlib 파서 — lxml 불필요
    for tag in soup(["script", "style", "nav", "head", "noscript"]):
        tag.decompose()
    lines: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        name = el.name
        if name[0] == "h" and name[1:].isdigit():
            lines.append("#" * int(name[1]) + " " + txt)
        elif name == "li":
            lines.append("- " + txt)
        else:
            lines.append(txt)
    text = "\n\n".join(lines) or soup.get_text("\n", strip=True)
    if len(text.strip()) < _MIN_CHARS:
        raise ExtractionError("html에서 텍스트를 얻지 못함")
    return text


def extract_text(
    path,
    *,
    max_chars: int = 200000,
    xlsx_max_rows: int = 200,
    ocr: bool = False,
    ocr_bin=None,
) -> str:
    """확장자에 따라 텍스트를 추출/읽어 반환. 실패 시 ExtractionError.

    - 텍스트 확장자 및 미지원 확장자: UTF-8로 그대로 읽음.
    - 문서 확장자: 해당 추출기 사용(모두 순수 파이썬, 오프라인).
    - PDF는 텍스트가 빈약하면 OCR로 폴백(ocr=True, vision-ocr 바이너리).
    - 이미지 확장자: OCR로 처리(ocr=True 필요).
    - max_chars 초과 시 잘라내고 안내 문구를 덧붙임.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".pdf":
        text = _extract_pdf(p, ocr=ocr, ocr_bin=ocr_bin)
    elif ext in IMAGE_EXTS:
        if not ocr:
            raise ExtractionError(f"이미지({ext})는 OCR 필요 — WIKI_INGEST_OCR=1 설정")
        text = _ocr_file(p, ocr_bin)
        if len(text.strip()) < _MIN_CHARS:
            raise ExtractionError("이미지에서 텍스트를 인식하지 못함")
    elif ext == ".docx":
        text = _extract_docx(p)
    elif ext == ".pptx":
        text = _extract_pptx(p)
    elif ext == ".xlsx":
        text = _extract_xlsx(p, max_rows=xlsx_max_rows)
    elif ext in (".html", ".htm"):
        text = _extract_html(p)
    else:
        text = p.read_text(encoding="utf-8", errors="replace")

    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n\n_(원문이 길어 일부만 사용됨)_"
    return text
