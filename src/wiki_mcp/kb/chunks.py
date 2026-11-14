"""추출 결과 조각 나누기 — 위치 표지·제목 단위, 약 1,500자 이하.

검색 색인의 단위다. 조각마다 마지막으로 지나친 위치 표지(PDF page · PPTX
slide · XLSX sheet · HWPX section)와 제목을 달아, 검색 결과에서 근거의
위치를 돌려줄 수 있게 한다.
"""

from __future__ import annotations

import re

MARKER_RE = re.compile(r"^<!--\s*((?:page|slide|sheet|section)\s+.+?)\s*-->$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

DEFAULT_MAX_CHARS = 1500


def split_chunks(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[dict]:
    """위치 표지·제목 경계로 조각을 나눈다.

    반환 항목: {"location": "section 0" | None, "heading": 제목 | None,
    "text": 조각 본문}. 경계 사이가 max_chars 를 넘으면 문단 경계에서 자르고,
    한 문단이 그마저 넘으면 하드 슬라이스한다.
    """
    chunks: list[dict] = []
    lines: list[str] = []
    length = 0
    location: str | None = None
    heading: str | None = None

    def flush() -> None:
        nonlocal lines, length
        body = "\n".join(lines).strip()
        if body:
            chunks.append({"location": location, "heading": heading, "text": body})
        lines = []
        length = 0

    for line in text.splitlines():
        stripped = line.strip()
        marker = MARKER_RE.match(stripped)
        if marker:
            flush()
            location = marker.group(1)
            continue
        head = HEADING_RE.match(line)
        if head:
            if length > 0:
                flush()
            heading = head.group(2).strip() or heading
            lines.append(line)
            length += len(line) + 1
            continue
        if length + len(line) + 1 > max_chars and length > 0:
            flush()
        if len(line) + 1 > max_chars:  # 한 줄이 상한을 넘는 경우 — 하드 슬라이스
            for i in range(0, len(line), max_chars):
                piece = line[i : i + max_chars]
                lines.append(piece)
                length = len(piece) + 1
                if i + max_chars < len(line):
                    flush()
            continue
        lines.append(line)
        length += len(line) + 1

    flush()
    return chunks
