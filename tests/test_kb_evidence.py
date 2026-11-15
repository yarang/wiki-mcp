"""근거 인용 검증 — locate_quote·context_around (설계 3.4절).

추출 텍스트의 공백 정규화(NFC + whitespace)로 인용을 찾고 offset·위치 표지를
돌려준다. offset 은 NFC 정규화한 텍스트 기준이다.
"""

from __future__ import annotations

import unicodedata

from wiki_mcp.kb.evidence import context_around, locate_quote

MARKER_TEXT = (
    "<!-- section 0 -->\n"
    "제1조(목적) 이 규정은 출장비 정산에 필요한 사항을 정한다.\n"
    "\n"
    "<!-- section 1 -->\n"
    "국내 출장 일비는 하루 2만 원으로 한다.\n"
)
PLAIN_TEXT = "제1조(목적) 이 규정은 출장비 정산에 필요한 사항을 정한다.\n국내 출장 일비는 하루 2만 원으로 한다.\n"


class TestLocateQuote:
    def test_exact_match_returns_normalized_quote_and_offsets(self):
        quote = "이 규정은 출장비 정산에 필요한"
        result = locate_quote(PLAIN_TEXT, quote)
        assert result is not None
        norm, start, end, location = result
        assert norm == quote
        assert PLAIN_TEXT[start:end] == quote
        assert end - start == len(quote)
        assert location is None  # 위치 표지가 없는 텍스트

    def test_whitespace_difference_is_match(self):
        """원문 줄바꿈 ↔ 인용 공백 하나는 같은 구절로 판정한다."""
        text = "국내 출장 일비는\n하루 2만 원으로 한다"
        quote = "출장 일비는 하루"
        result = locate_quote(text, quote)
        assert result is not None
        norm, start, end, _ = result
        assert norm == quote
        # offset 구간은 정규화 텍스트 기준으로 해당 구절을 담는다
        assert unicodedata.normalize("NFC", text)[start:end].replace("\n", " ") == quote

    def test_quote_with_extra_spaces_matches(self):
        text = "국내  출장\t일비는 하루 2만 원"
        quote = "국내 출장 일비는 하루 2만"
        assert locate_quote(text, quote) is not None

    def test_not_found_returns_none(self):
        assert locate_quote("전혀 다른 내용의 문서입니다.", "이 구절은 문서에 없다") is None

    def test_location_is_last_marker_before_match(self):
        quote = "국내 출장 일비는 하루 2만 원으로 한다"
        result = locate_quote(MARKER_TEXT, quote)
        assert result is not None
        *_, location = result
        assert location == "section 1"

    def test_location_none_before_any_marker(self):
        quote = "이 규정은 출장비 정산에 필요한"
        result = locate_quote(MARKER_TEXT, quote)
        assert result is not None
        *_, location = result
        assert location == "section 0"

    def test_nfd_text_is_normalized_to_nfc(self):
        """추출 텍스트가 NFD로 저장돼도 NFC 인용을 찾는다(R5)."""
        nfd_text = unicodedata.normalize("NFD", "국내 출장 일비는 하루 2만 원으로 한다")
        quote = "국내 출장 일비는 하루"  # NFC
        result = locate_quote(nfd_text, quote)
        assert result is not None
        norm, start, end, _ = result
        assert norm == quote
        assert unicodedata.normalize("NFC", nfd_text)[start:end] == quote


class TestContextAround:
    def test_before_match_after_with_boundaries(self):
        text = "앞부분 문장이다. " + "가" * 100 + " 뒷부분 문장이다."
        start = text.index("앞부분") + len("앞부분 문장이다. ")
        end = start + 10
        ctx = context_around(text, start, end, 5)
        assert ctx["match"] == text[start:end]
        assert ctx["before"] == text[start - 5 : start]
        assert ctx["after"] == text[end : end + 5]
        assert ctx["truncated_before"] is True
        assert ctx["truncated_after"] is True

    def test_no_truncation_at_text_edges(self):
        text = "처음부터 끝까지 짧은 문장"
        start, end = 0, 4
        ctx = context_around(text, start, end, 100)
        assert ctx["before"] == ""
        assert ctx["after"] == text[end:]
        assert ctx["truncated_before"] is False
        assert ctx["truncated_after"] is False
