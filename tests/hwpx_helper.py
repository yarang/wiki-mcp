"""테스트용 HWPX 표본 조립 헬퍼 — 실제 한글(HWPML) 구조를 본뜬 최소 XML.

실제 한글 문서에서 확인한 구조(검증 방법: 실제 .hwpx 표본 압축 해제 후
section*.xml 확인 — WIKI_MCP_REAL_HWPX_DIR 환경 변수로 실제 파일 테스트):
- 섹션: `hs:sec`(2011/section 네임스페이스) 아래 `hp:p` 문단이 온다.
- 문단: `hp:p > hp:run > hp:t` — 텍스트는 run 안의 t 에 든다.
- 혼합 콘텐츠: `hp:tab`·`hp:lineBreak` 가 **hp:t 안에** 중첩되고, 그 뒤
  텍스트는 요소의 꼬리(.tail)로 온다. `<hp:t>가<hp:lineBreak/>나다</hp:t>`
  처럼 한 t 안에 텍스트+줄바꿈+텍스트가 섞인다.
- 표: 문단 안에 `hp:p > hp:run > hp:tbl > hp:tr > hp:tc > hp:subList > hp:p`
  로 중첩된다(섹션 직속 tbl 도 있다).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

HP_NS = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS_NS = "http://www.hancom.co.kr/hwpml/2011/section"


def t(*parts: str) -> str:
    """hp:t — 문자열·tab()·br() 조각을 섞어 담는 혼합 콘텐츠 텍스트."""
    return f"<hp:t>{''.join(parts)}</hp:t>"


def p(*runs: str) -> str:
    """hp:p 문단 — 자식을 hp:run 하나로 감싼다(실제 문서의 기본형)."""
    return f"<hp:p><hp:run>{''.join(runs)}</hp:run></hp:p>"


def p_raw(inner: str) -> str:
    """hp:p 문단 — 내부 XML 을 그대로(문단 안 표 등 복합 구조 조립용)."""
    return f"<hp:p><hp:run>{inner}</hp:run></hp:p>"


def tab() -> str:
    return "<hp:tab/>"


def br() -> str:
    return "<hp:lineBreak/>"


def cell(*paras: str) -> str:
    return f"<hp:tc><hp:subList>{''.join(paras)}</hp:subList></hp:tc>"


def row(*cells: str) -> str:
    return f"<hp:tr>{''.join(cells)}</hp:tr>"


def tbl(*rows: str) -> str:
    return f"<hp:tbl>{''.join(rows)}</hp:tbl>"


def sec(body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<hs:sec xmlns:hs="{HS_NS}" xmlns:hp="{HP_NS}" type="body" id="1">'
        f"{body}</hs:sec>"
    )


def make_hwpx(path: Path, sections: dict[str, str]) -> Path:
    """실제 한글 문서처럼 DEFLATE 압축으로 zip을 만든다."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, xml in sections.items():
            zf.writestr(name, xml)
    return path
