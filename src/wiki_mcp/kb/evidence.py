"""근거 인용 검증 — 추출 텍스트에서 인용 구절 찾기(설계 3.4절).

인용(quote)은 추출 텍스트에 실제로 있어야 한다. 비교는 둘 다 NFC 로
정규화하고 공백(isspace 문자열)을 하나로 묶은 뒤 한다 — 원문 줄바꿈과 인용
공백 하나를 같은 구절로 본다. offset 은 **NFC 정규화한 텍스트 기준**이다
(추출 파일이 NFD 라면 read_extract 의 offset 과 다를 수 있다 — R5).
"""

from __future__ import annotations

import unicodedata

from wiki_mcp.kb.chunks import MARKER_RE


def _normalize_ws(text: str) -> tuple[str, list[int]]:
    """공백 문자열을 공백 하나로 묶는다. (정규화 텍스트, 원본 인덱스 대응표).

    대응표는 정규화 텍스트의 각 글자가 원본 텍스트의 어느 인덱스에서 왔는지
    담는다. 묶은 공백은 첫 번째 공백 문자의 인덱스를 가리킨다.
    """
    out: list[str] = []
    mapping: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            out.append(" ")
            mapping.append(i)
            while i < n and text[i].isspace():
                i += 1
        else:
            out.append(text[i])
            mapping.append(i)
            i += 1
    return "".join(out), mapping


def _strip_edges(norm: str, mapping: list[int]) -> tuple[str, list[int]]:
    """정규화 결과의 앞뒤 공백을 대응표와 함께 뗀다."""
    start = 0
    end = len(norm)
    if norm.startswith(" "):
        start = 1
    if norm.endswith(" ") and end - start > 0:
        end -= 1
    return norm[start:end], mapping[start:end]


def normalize_text(text: str) -> tuple[str, list[int]]:
    """검증 기준 텍스트 — NFC + 공백 묶음 + 앞뒤 공백 제거와 원본 인덱스 대응표."""
    nfc = unicodedata.normalize("NFC", text)
    norm, mapping = _normalize_ws(nfc)
    return _strip_edges(norm, mapping)


def normalize_quote(quote: str) -> str:
    """인용의 정규화 형태 — 텍스트와 같은 규칙(NFC + 공백 묶음 + 가장자리 제거)."""
    norm, _ = normalize_text(quote)
    return norm


def locate_quote(extract_text: str, quote: str) -> tuple[str, int, int, str | None] | None:
    """추출 텍스트에서 인용을 찾는다.

    반환: (정규화된 quote, offset_start, offset_end, location | None).
    offset 은 NFC 정규화 텍스트 기준. 못 찾으면 None. location 은 offset_start
    이전 줄 중 위치 표지(<!-- page 3 --> 등)에 맞는 마지막 줄의 표지 문자열.
    """
    text, mapping = normalize_text(extract_text)
    q = normalize_quote(quote)
    if not q:
        return None
    pos = text.find(q)
    if pos < 0:
        return None
    offset_start = mapping[pos]
    offset_end = mapping[pos + len(q) - 1] + 1
    # 위치 표지는 줄 단위로 읽어야 한다 — 공백을 묶기 전 NFC 원본에서 찾는다.
    nfc = unicodedata.normalize("NFC", extract_text)
    return q, offset_start, offset_end, _location_before(nfc, offset_start)


def _location_before(text: str, offset: int) -> str | None:
    """offset 이전 줄 중 위치 표지에 맞는 마지막 줄의 표지(group 1)."""
    for line in reversed(text[:offset].splitlines()):
        marker = MARKER_RE.match(line.strip())
        if marker:
            return marker.group(1)
    return None


def context_around(text: str, start: int, end: int, chars: int) -> dict:
    """match 구간의 앞뒤 문맥. truncated_* 는 실제 텍스트가 잘렸는지 뜻한다."""
    c = max(0, int(chars))
    s = max(0, min(start, len(text)))
    e = max(s, min(end, len(text)))
    return {
        "before": text[max(0, s - c) : s],
        "match": text[s:e],
        "after": text[e : e + c],
        "truncated_before": s - c > 0,
        "truncated_after": e + c < len(text),
    }
