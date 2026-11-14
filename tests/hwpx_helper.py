"""테스트용 HWPX 표본 조립 헬퍼 — 실제 한글(HWPML) 구조를 zip으로 직접 만든다."""

from __future__ import annotations

import zipfile
from pathlib import Path

HP_NS = "http://www.hancom.co.kr/hwpml/2016/core"
HS_NS = "http://www.hancom.co.kr/hwpml/2016/section"


def t(text: str) -> str:
    return f"<hp:t>{text}</hp:t>"


def p(*runs: str) -> str:
    return f"<hp:p><hp:r>{''.join(runs)}</hp:r></hp:p>"


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
    with zipfile.ZipFile(path, "w") as zf:
        for name, xml in sections.items():
            zf.writestr(name, xml)
    return path
