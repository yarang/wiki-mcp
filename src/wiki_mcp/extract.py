"""문서 텍스트 추출 — 완전 오프라인(에어갭).

open-webui가 명시 의존성으로 포함하는 순수 파이썬 라이브러리만 사용한다
(pypdf, docx2txt, python-pptx, openpyxl, beautifulsoup4). 네트워크·모델
다운로드 없음. bs4는 stdlib html.parser를 사용해 lxml에 의존하지 않는다.

지원: .pdf .docx .pptx .xlsx .html/.htm .hwpx  (그 외/텍스트는 그대로 읽음)
미지원: 스캔(이미지) PDF — 텍스트가 없어 OCR이 필요하나 에어갭 부담으로 제외.

0.3.0: markers=True면 위치 표지(PDF 페이지·PPTX 슬라이드·XLSX 시트·HWPX 섹션)를
본문에 함께 반환한다. 기본값 False — 기존(번들) 추출 결과는 바뀌지 않는다.
ExtractionError.code는 KB 스캔이 상태 코드(encrypted/corrupt/empty_text/…)로
매핑하는 데 쓴다.
"""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

# 그대로 읽는 텍스트 확장자
TEXT_EXTS = {".txt", ".md", ".markdown", ".text"}
# OCR로 처리하는 이미지 확장자
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".heic"}

_MIN_CHARS = 20  # 유의미 텍스트 최소 길이 (미만이면 추출 실패로 간주)

# zip 기반 문서(HWPX·DOCX·PPTX·XLSX) 안전 상한 — 파싱 전에 zip 메타데이터로
# 검사한다(zip bomb 방어, M13). 항목 수·해제 합·항목당 압축비.
_ZIP_MAX_ENTRIES = 1000
_ZIP_MAX_UNCOMPRESSED = 50 * 1024 * 1024
_ZIP_MAX_RATIO = 100
# 직접 파싱하는 XML에서 거부하는 마커(XXE 방어 — ElementTree 는 외부 엔티티를
# 해석하지 않지만 내부 엔티티 확장으로 불릴 수 있으니 아예 거부한다)
_XML_FORBIDDEN_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")
_HWPX_SECTION_RE = re.compile(r"^Contents/section(\d+)\.xml$")


def _check_zip_safety(path: Path) -> None:
    """zip 기반 문서의 폭탄 방어(M13) — 항목 수·해제 합·압축비 상한.

    DOCX·PPTX·XLSX·HWPX 모두 파싱(라이브러리 import·랜더링) 전에 이 검사를
    지난다. 위반하면 ExtractionError(corrupt).
    """
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > _ZIP_MAX_ENTRIES:
                raise ExtractionError(f"zip 항목 수 상한 초과({len(infos)}개)", code="corrupt")
            total = 0
            for info in infos:
                total += info.file_size
                if total > _ZIP_MAX_UNCOMPRESSED:
                    raise ExtractionError(f"zip 해제 크기 상한 초과({total}B)", code="corrupt")
                if info.compress_size > 0 and info.file_size / info.compress_size > _ZIP_MAX_RATIO:
                    raise ExtractionError(
                        f"zip 항목 압축비 상한 초과({info.filename}: "
                        f"{info.file_size}/{info.compress_size})",
                        code="corrupt",
                    )
    except zipfile.BadZipFile as e:
        raise ExtractionError(f"zip 문서가 아니다: {e}", code="corrupt") from e
    except ExtractionError:
        raise
    except Exception as e:  # zipfile 내부 오류도 corrupt 로 통일
        raise ExtractionError(f"zip 검사 실패: {e}", code="corrupt") from e


class ExtractionError(Exception):
    """문서에서 유효한 텍스트를 얻지 못함 (라이브러리 없음/스캔본/파싱 실패 등).

    code는 KB 스캔의 고정 실패 사유 코드:
    unsupported / encrypted / corrupt / empty_text / extract_error
    (too_large·locked·unstable은 스캔 단계에서 판정한다.)
    """

    def __init__(self, message: str, code: str = "extract_error"):
        super().__init__(message)
        self.code = code


def _ocr_file(path: Path, ocr_bin, timeout: int = 60) -> str:
    """vision-ocr 바이너리(Apple Vision)로 PDF/이미지에서 텍스트 인식. 오프라인.

    timeout 기본 60초(H5) — 스캔 한 장이 이벤트 루프를 오래 붙잡지 않게.
    """
    if not ocr_bin or not Path(ocr_bin).exists():
        raise ExtractionError("OCR 바이너리 없음(vision-ocr) — OCR 불가")
    try:
        r = subprocess.run(
            [str(ocr_bin), str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ExtractionError("OCR 시간 초과")
    except Exception as e:
        raise ExtractionError(f"OCR 실행 실패: {e}")
    if r.returncode != 0:
        raise ExtractionError(f"OCR 실패: {(r.stderr or '').strip()[:200]}")
    return r.stdout


def _pdf_collect_pages(reader) -> list[tuple[int, str]]:
    """reader에서 텍스트가 있는 페이지를 (번호, 텍스트) 목록으로 뽑는다.

    빈 페이지는 건너뛰되 페이지 번호는 원본 그대로 유지한다(위치 표지용).
    """
    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            pages.append((i, t.strip()))
    return pages


def _pdf_pages(path: Path) -> list[tuple[int, str]]:
    """텍스트가 있는 페이지를 (번호, 텍스트) 목록으로 반환. markers·OCR 공용."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractionError("pypdf 미설치 — PDF 추출 불가")
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise ExtractionError(f"PDF 열기 실패: {e}", code="corrupt")
    if getattr(reader, "is_encrypted", False):
        raise ExtractionError("암호로 보호된 PDF", code="encrypted")
    return _pdf_collect_pages(reader)


def _extract_pdf(path: Path, *, ocr: bool = False, ocr_bin=None, markers: bool = False) -> str:
    pages = _pdf_pages(path)
    if markers:
        text = "\n\n".join(f"<!-- page {num} -->\n{t}" for num, t in pages)
    else:
        text = "\n\n".join(t for _, t in pages)
    if len(text.strip()) >= _MIN_CHARS:
        return text

    # 텍스트가 빈약함 = 스캔/이미지 PDF → OCR 폴백
    if ocr and ocr_bin and Path(ocr_bin).exists():
        ocr_text = _ocr_file(path, ocr_bin)
        if len(ocr_text.strip()) >= _MIN_CHARS:
            return ocr_text
        raise ExtractionError("스캔 PDF로 추정되나 OCR로도 텍스트를 얻지 못함", code="empty_text")
    raise ExtractionError(
        "추출 텍스트가 거의 없음 — 스캔/이미지 PDF로 추정. "
        "OCR을 켜거나(WIKI_INGEST_OCR=1) 텍스트 PDF로 변환 후 재투입 필요.",
        code="empty_text",
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
        raise ExtractionError("docx에서 텍스트를 얻지 못함", code="empty_text")
    return text


def _extract_pptx(path: Path, *, markers: bool = False) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        raise ExtractionError("python-pptx 미설치 — pptx 추출 불가")
    try:
        prs = Presentation(str(path))
    except Exception as e:
        raise ExtractionError(f"pptx 열기 실패: {e}", code="corrupt")
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
        slide_text = heading + ("\n\n" + "\n".join(body) if body else "")
        out.append(f"<!-- slide {i} -->\n{slide_text}" if markers else slide_text)
    text = "\n\n".join(out)
    if len(text.strip()) < _MIN_CHARS:
        raise ExtractionError("pptx에서 텍스트를 얻지 못함", code="empty_text")
    return text


def _extract_xlsx(path: Path, max_rows: int = 200, *, markers: bool = False) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ExtractionError("openpyxl 미설치 — xlsx 추출 불가")
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        raise ExtractionError(f"xlsx 열기 실패: {e}", code="corrupt")
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
        sheet_md = "\n".join(md)
        out.append(f"<!-- sheet {ws.title} -->\n{sheet_md}" if markers else sheet_md)
    wb.close()
    text = "\n\n".join(out)
    if len(text.strip()) < _MIN_CHARS:
        raise ExtractionError("xlsx에서 데이터를 얻지 못함", code="empty_text")
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
        raise ExtractionError("html에서 텍스트를 얻지 못함", code="empty_text")
    return text


# ─── HWPX (0.3.0) ────────────────────────────────────────────────────
def _local(tag: str) -> str:
    """'{namespace}local' 형태의 ElementTree 태그에서 로컬 이름만 뺀다."""
    return tag.rsplit("}", 1)[-1]


def _hwpx_emit_inline(el, parts: list[str], tables: list) -> None:
    """요소의 인라인 텍스트를 문서 순서대로 뽑는다(H2 — 실제 구조).

    실제 한글 문서는 hp:p > hp:run > hp:t 이고, hp:tab·hp:lineBreak 가
    **hp:t 안에** 중첩되며 그 뒤 텍스트는 꼬리(.tail)로 온다
    (`<hp:t>가<hp:lineBreak/>나다</hp:t>`). text → 자식 → tail 순서로
    재귀하며 둘 다 보존한다. 문단 안의 hp:tbl 은 tables 에 따로 모은다.
    run·t 밖의 요소(secPr, linesegarray 등 서식 정의)에는 텍스트가 없으니
    건너뛴다.
    """
    if el.text:
        parts.append(el.text)
    for child in el:
        name = _local(child.tag)
        if name == "t":
            _hwpx_emit_inline(child, parts, tables)
        elif name == "tab":
            parts.append("\t")
        elif name == "lineBreak":
            parts.append("\n")
        elif name == "tbl":
            tables.append(child)
        elif name == "run":  # hp:run — 인라인 컨테이너
            _hwpx_emit_inline(child, parts, tables)
        if child.tail:
            parts.append(child.tail)


def _hwpx_inline_text(el) -> str:
    """요소의 인라인 텍스트 전체(표 제외). hp:tab→탭, hp:lineBreak→줄바꿈."""
    parts: list[str] = []
    _hwpx_emit_inline(el, parts, [])
    return "".join(parts)


def _hwpx_paragraph_lines(p_el) -> list[str]:
    """문단 하나를 줄 목록으로 — 문단 텍스트 한 줄 + 문단 안 표 줄들(H2).

    실제 문서는 표가 hp:p > hp:run > hp:tbl 로 문단 안에 중첩된다. 문단
    텍스트와 표를 각각의 줄로 분리해 순서를 보존한다.
    """
    parts: list[str] = []
    tables: list = []
    _hwpx_emit_inline(p_el, parts, tables)
    lines = ["".join(parts)]
    for tbl in tables:
        lines.extend(_hwpx_table_lines(tbl))
    return lines


def _hwpx_table_lines(tbl) -> list[str]:
    """hp:tbl을 행마다 셀을 ' | '로 이은 줄 목록으로 만든다."""
    lines: list[str] = []

    def walk(e) -> None:
        for child in e:
            name = _local(child.tag)
            if name == "tr":
                cells = [_hwpx_cell_text(c) for c in child if _local(c.tag) == "tc"]
                lines.append(" | ".join(cells))
            elif name == "tbl":  # 중첩 표도 같은 규칙으로 평면화
                lines.extend(_hwpx_table_lines(child))
            else:
                walk(child)

    walk(tbl)
    return lines


def _hwpx_cell_text(tc) -> str:
    """hp:tc 셀 하나의 텍스트 — 셀 안 줄바꿈·문단은 공백으로 이어 한 줄로."""

    def walk(e, out: list[str]) -> None:
        for child in e:
            name = _local(child.tag)
            if name == "p":
                text = " ".join(_hwpx_inline_text(child).split())
                if text:
                    out.append(text)
            elif name == "tbl":
                out.extend(_hwpx_table_lines(child))
            else:
                walk(child, out)

    parts: list[str] = []
    walk(tc, parts)
    return " ".join(parts)


def _hwpx_xml_to_text(data: bytes) -> str:
    # XXE·엔티티 확장 방어(M13) — DTD·ENTITY 선언이 있으면 파싱 전에 거부한다.
    # ElementTree는 외부 엔티티를 해석하지 않지만 내부 엔티티 폭탄은 막지 못한다.
    head = data[:4096]
    if any(marker in head for marker in _XML_FORBIDDEN_MARKERS):
        raise ExtractionError("hwpx XML에 DTD/ENTITY 선언이 있다", code="corrupt")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ExtractionError(f"hwpx XML 파싱 실패: {e}", code="corrupt")
    lines: list[str] = []

    def walk(e) -> None:
        for child in e:
            name = _local(child.tag)
            if name == "p":
                lines.extend(_hwpx_paragraph_lines(child))
            elif name == "tbl":
                lines.extend(_hwpx_table_lines(child))
            else:
                walk(child)

    walk(root)
    return "\n".join(lines)


def _extract_hwpx(path: Path, *, markers: bool = False) -> str:
    """HWPX(OPC zip)에서 Contents/section*.xml 본문을 뽑는다.

    - 섹션은 파일명 숫자 오름차순(section10은 section2 뒤).
    - zip 상한(항목 수·해제 합·압축비)은 공용 _check_zip_safety 가 파싱 전에
      검사한다(M13) — extract_text 진입점에서 호출된다.
    - 바이너리 .hwp는 여기서 다루지 않는다(unsupported).
    """
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            sections: list[tuple[int, str]] = []
            for info in zf.infolist():
                m = _HWPX_SECTION_RE.match(info.filename)
                if m:
                    sections.append((int(m.group(1)), info.filename))
            if not sections:
                raise ExtractionError("hwpx에 본문 섹션 없음", code="empty_text")
            sections.sort()
            parts: list[str] = []
            for num, name in sections:
                text = _hwpx_xml_to_text(zf.read(name))
                parts.append(f"<!-- section {num} -->\n{text}" if markers else text)
    except zipfile.BadZipFile as e:
        raise ExtractionError(f"hwpx 열기 실패: {e}", code="corrupt") from e

    text = "\n".join(parts)
    if len(text.strip()) < _MIN_CHARS:
        raise ExtractionError("hwpx에서 텍스트를 얻지 못함", code="empty_text")
    return text


def extract_text(
    path,
    *,
    max_chars: int = 200000,
    xlsx_max_rows: int = 200,
    ocr: bool = False,
    ocr_bin=None,
    markers: bool = False,
) -> str:
    """확장자에 따라 텍스트를 추출/읽어 반환. 실패 시 ExtractionError.

    - 텍스트 확장자 및 미지원 확장자: UTF-8로 그대로 읽음.
    - 문서 확장자: 해당 추출기 사용(모두 순수 파이썬, 오프라인).
    - PDF는 텍스트가 빈약하면 OCR로 폴백(ocr=True, vision-ocr 바이너리).
    - 이미지 확장자: OCR로 처리(ocr=True 필요).
    - markers=True면 위치 표지(PDF page/PPTX slide/XLSX sheet/HWPX section)를
      본문에 붙인다. 기본 False — 기존 추출 결과는 그대로.
    - max_chars 초과 시 잘라내고 안내 문구를 덧붙임.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".pdf":
        text = _extract_pdf(p, ocr=ocr, ocr_bin=ocr_bin, markers=markers)
    elif ext in IMAGE_EXTS:
        if not ocr:
            raise ExtractionError(f"이미지({ext})는 OCR 필요 — WIKI_INGEST_OCR=1 설정")
        text = _ocr_file(p, ocr_bin)
        if len(text.strip()) < _MIN_CHARS:
            raise ExtractionError("이미지에서 텍스트를 인식하지 못함", code="empty_text")
    elif ext == ".docx":
        _check_zip_safety(p)
        text = _extract_docx(p)
    elif ext == ".pptx":
        _check_zip_safety(p)
        text = _extract_pptx(p, markers=markers)
    elif ext == ".xlsx":
        _check_zip_safety(p)
        text = _extract_xlsx(p, max_rows=xlsx_max_rows, markers=markers)
    elif ext in (".html", ".htm"):
        text = _extract_html(p)
    elif ext == ".hwpx":
        _check_zip_safety(p)
        text = _extract_hwpx(p, markers=markers)
    else:
        text = p.read_text(encoding="utf-8", errors="replace")

    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n\n_(원문이 길어 일부만 사용됨)_"
    return text
