"""초안 입력·저장값 검증 (설계 2.2·3.4·3.5절) — drafts.py 에서 분리.

검증은 kb_lock 과 무관한 순수 검사(형식·길이·제어문자)와 manifest·추출
파일을 읽는 근거 검증으로 이루어진다. 순수 검사는 잠금 밖에서 먼저 돌려
잠금을 짧게 쥔다(0.4.0 보안 리뷰 1 — 긴 입력이 find_notes·resolve 를
멈추게 하는 일을 막는다).
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import frontmatter
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.evidence import locate_quote, normalize_quote
from wiki_mcp.kb.store import KnowledgeBase, normalize_rel_path, rel_key

Actor = Literal["cli", "mcp"]
MAX_BODY_CHARS = 20_000
MAX_PENDING = 200
MAX_EVIDENCE = 10
MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 300
MAX_REASON_CHARS = 500
MAX_DOMAIN_CHARS = 100
MAX_NOTE_ID_CHARS = 80
CURRENT_NOTE_CHARS = 100_000
LIST_LIMIT = 200
DRAFT_ID_RE = re.compile(r"d-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")

# 제어문자 — C0·C1(±U+0085)·U+2028·U+2029. 한 줄 값(머리말·태그·요약)에는
# \t\n\r 포함 전부를 거부하고, 본문에는 \t\n\r 만 허용한다.
_LINE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BODY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# 양방향 제어문자·보이지 않는 문자 — 표시를 속여 보이는 내용과 실제 내용을
# 다르게 만든다. 한 줄 값에는 전부 거부하고, 본문·인용에는 bidi 오버라이드만
# 거부한다(제로폭 공백 등은 실제 문서에 있을 수 있다).
_BIDI_RE = re.compile(r"[‪-‮⁦-⁩]")
_INVISIBLE_RE = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")

SOURCES_START = "<!-- kb:sources:start -->"
SOURCES_END = "<!-- kb:sources:end -->"

_TAG_RE = re.compile(r"[0-9a-z가-힣][0-9a-z가-힣_-]{0,39}")
_TAG_FORBIDDEN = {"true", "false", "yes", "no", "on", "off", "null", "y", "n"}


class EvidenceInput(BaseModel):
    """LLM/CLI 가 제출하는 근거 — sha256·offset·location 은 서버가 채운다."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    rel_path: str
    quote: str


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["new", "update"]
    note_id: str
    body: str
    evidence: list[EvidenceInput]
    title: str = ""
    tags: list[str] = []
    domain: str = ""
    summary: str = ""


class StoredEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_id: str
    rel_path: str
    sha256: str
    quote: str
    location: str | None = None
    offset_start: int
    offset_end: int


class StoredDraft(BaseModel):
    """초안 파일의 저장 형식(schema==1). 손상 시 corrupt_draft."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: Literal["new", "update"]
    note_id: str
    title: str
    domain: str
    tags: list[str]
    summary: str
    body: str
    created_at: str
    created_by: str
    base: dict | None = None
    evidence: list[StoredEvidence]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_token() -> str:
    import secrets

    return secrets.token_hex(3)


# ─── 필드 검증 (3.5절 1·2·3·4단계) ───────────────────────────────────
def _check_line_value(value: str, field: str, *, max_chars: int, min_chars: int = 0) -> None:
    if not min_chars <= len(value) <= max_chars:
        raise KBError("invalid_draft", f"{field} 길이가 범위를 벗어난다({len(value)}자)")
    if _LINE_CONTROL_RE.search(value) or _INVISIBLE_RE.search(value):
        raise KBError("invalid_draft", f"{field}에 제어문자를 넣을 수 없다")


def _validate_tag(tag: str) -> None:
    if tag.lower() in _TAG_FORBIDDEN:
        raise KBError("invalid_draft", f"태그 규칙에 맞지 않는다: {tag!r}")
    if not _TAG_RE.fullmatch(tag) or tag[0].isdigit():
        raise KBError("invalid_draft", f"태그 규칙에 맞지 않는다: {tag!r}")
    # YAML 태그 리스트로 왕복하지 않는 값(숫자·날짜·bool)은 노트 머리말에서
    # 문자열이 아니게 되므로 거부한다(writer.build_frontmatter 는 따옴표 없이
    # 쓴다). 개행 등으로 끝나는 값도 여기서 걸린다.
    if yaml.safe_load(f"[{tag}]") != [tag]:
        raise KBError("invalid_draft", f"태그가 YAML 문자열로 왕복하지 않는다: {tag!r}")


def check_reason(reason: str) -> None:
    """반려 사유 — 0~500자, 한 줄 제어문자·보이지 않는 문자 금지."""
    if len(reason) > MAX_REASON_CHARS:
        raise KBError("invalid_draft", f"반려 사유는 {MAX_REASON_CHARS}자 이하여야 한다")
    if _LINE_CONTROL_RE.search(reason) or _INVISIBLE_RE.search(reason):
        raise KBError("invalid_draft", "반려 사유에 제어문자가 있다")


def validate_fields(data: DraftInput) -> None:
    """순수 필드 검증 — 파일 시스템을 보지 않으므로 잠금 밖에서 먼저 돈다."""
    if data.kind == "new":
        _check_line_value(data.title, "title", max_chars=MAX_TITLE_CHARS, min_chars=1)
    _check_line_value(data.summary, "summary", max_chars=MAX_SUMMARY_CHARS)
    if not 1 <= len(data.note_id) <= MAX_NOTE_ID_CHARS:
        raise KBError("invalid_note_id", f"note_id 길이가 범위를 벗어난다({len(data.note_id)}자)")
    if data.domain and len(data.domain) > MAX_DOMAIN_CHARS:
        raise KBError("invalid_draft", f"domain 은 {MAX_DOMAIN_CHARS}자 이하여야 한다")
    if not 1 <= len(data.evidence) <= MAX_EVIDENCE:
        raise KBError(
            "invalid_draft", f"근거는 1~{MAX_EVIDENCE}개여야 한다({len(data.evidence)}개)"
        )
    for i, ev in enumerate(data.evidence):
        if not 1 <= len(ev.source_id) <= 64:
            raise KBError("invalid_draft", f"evidence[{i}].source_id 길이 오류")
        if not 1 <= len(ev.rel_path) <= 1024:
            raise KBError("invalid_draft", f"evidence[{i}].rel_path 길이 오류")
        if not 1 <= len(ev.quote) <= 2000:
            raise KBError("invalid_draft", f"evidence[{i}].quote 길이 오류(원 입력 1~2000자)")
    if len(data.tags) > 10:
        raise KBError("invalid_draft", "태그는 최대 10개다")
    for tag in data.tags:
        _validate_tag(tag)
    _validate_body(data.body)


def _validate_body(body: str) -> None:
    if not 1 <= len(body) <= MAX_BODY_CHARS:
        if len(body) > MAX_BODY_CHARS:
            raise KBError("draft_too_large", f"본문이 {MAX_BODY_CHARS}자를 넘는다({len(body)}자)")
        raise KBError("invalid_draft", "본문은 비어 있을 수 없다")
    if _BODY_CONTROL_RE.search(body) or _BIDI_RE.search(body):
        raise KBError("invalid_draft", "본문에 허용되지 않는 제어문자가 있다")
    if SOURCES_START in body or SOURCES_END in body:
        raise KBError("invalid_draft", "본문에 출처 절 예약 표지를 넣을 수 없다")


def revalidate_stored(stored: StoredDraft, draft_id: str) -> None:
    """승인 직전 재검증 — 저장값을 저장 때와 같은 규칙으로 다시 거른다.

    초안 파일이 손으로 고쳐 들어온 경우 승인 경로(머리말·경로 생성)가
    검증 없는 값에 도달하지 않게 한다. 파일명 id 와 내용 id 가 다른
    경우도 corrupt_draft 다.
    """
    from wiki_mcp.writer import validate_domain, validate_note_id

    if stored.id != draft_id or not DRAFT_ID_RE.fullmatch(stored.id):
        raise KBError("corrupt_draft", f"초안 id 가 형식에 맞지 않다: {stored.id!r}")
    try:
        if validate_note_id(stored.note_id) != stored.note_id:
            raise KBError("invalid_note_id", f"note_id 가 정규화로 바뀐다: {stored.note_id!r}")
    except ValueError as e:
        raise KBError("invalid_note_id", str(e)) from e
    try:
        if stored.domain:
            # note_id 과 같이 정규화로 바뀌는 값(빈 세그먼트·앞뒤 공백)은
            # 저장값과 달라 승인 경로에 원값이 남지 않게 거부한다(N2).
            if validate_domain(stored.domain) != stored.domain:
                raise KBError("invalid_domain", f"domain 이 정규화로 바뀐다: {stored.domain!r}")
    except ValueError as e:
        raise KBError("invalid_domain", str(e)) from e
    _check_line_value(stored.title, "title", max_chars=MAX_TITLE_CHARS)
    _check_line_value(stored.summary, "summary", max_chars=MAX_SUMMARY_CHARS)
    for tag in stored.tags:
        _validate_tag(tag)
    _validate_body(stored.body)


# ─── 근거 검증 (3.4절) ───────────────────────────────────────────────
def _evidence_entry(kb: KnowledgeBase, ev: EvidenceInput, index: dict, ordinal: int) -> dict:
    """근거 원본 항목 검사 — manifest 등록·ok·추출 파일 존재. 통과하면 entry."""
    for value in (ev.rel_path, ev.source_id):
        if _LINE_CONTROL_RE.search(value) or _BIDI_RE.search(value) or _INVISIBLE_RE.search(value):
            raise KBError(
                "evidence_source_invalid",
                f"근거 {ordinal}의 경로에 제어문자가 있다",
            )
    rel = normalize_rel_path(str(ev.rel_path).replace("\\", "/"))
    entry = index.get((ev.source_id, rel_key(rel)))
    if entry is None or entry.get("status") != "ok" or not entry.get("sha256"):
        raise KBError(
            "evidence_source_invalid",
            f"근거 {ordinal}({ev.source_id} {ev.rel_path})가 manifest 의 추출 성공 항목이 아니다",
        )
    if not kb.extract_path(entry["sha256"]).exists():
        raise KBError(
            "evidence_source_invalid",
            f"근거 {ordinal}의 추출 파일이 없다 — scan 으로 다시 동기화한다",
        )
    return entry


def validate_evidence(
    kb: KnowledgeBase, ev: EvidenceInput, index: dict, ordinal: int
) -> StoredEvidence:
    """근거 원본 검사 + 인용 검증. 검사를 통과하면 저장용 항목을 돌려준다."""
    entry = _evidence_entry(kb, ev, index, ordinal)
    quote = normalize_quote(ev.quote)
    if not 8 <= len(quote) <= 500:
        raise KBError(
            "invalid_draft", f"근거 {ordinal}의 인용은 정규화 뒤 8~500자여야 한다({len(quote)}자)"
        )
    if _LINE_CONTROL_RE.search(quote) or _BIDI_RE.search(quote):
        raise KBError("invalid_draft", f"근거 {ordinal}의 인용에 제어문자가 있다")
    located = locate_quote(frontmatter.load(kb.extract_path(entry["sha256"])).content, ev.quote)
    if located is None:
        raise KBError(
            "evidence_not_found",
            f"근거 {ordinal}의 인용이 추출 텍스트에 없다 — 원문 구절을 그대로 옮긴다",
        )
    norm, offset_start, offset_end, location = located
    return StoredEvidence(
        source_id=ev.source_id,
        rel_path=entry["rel_path"],
        sha256=entry["sha256"],
        quote=norm,
        location=location,
        offset_start=offset_start,
        offset_end=offset_end,
    )


# ─── 저장 파일 읽기 ──────────────────────────────────────────────────
def load_stored(path: Path) -> StoredDraft:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise KBError("corrupt_draft", f"초안 파일을 파싱하지 못했다: {path.name} ({e})") from e
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise KBError("corrupt_draft", f"초안 파일 형식이 잘못됐다: {path.name}")
    try:
        return StoredDraft.model_validate(data)
    except ValidationError as e:
        raise KBError("corrupt_draft", f"초안 파일 필드가 잘못됐다: {path.name} ({e})") from e


def iter_pending(kb: KnowledgeBase) -> tuple[list[StoredDraft], list[str]]:
    """(대기 초안 목록, 손상 id 목록). created_at 오름차순(FIFO), 같으면 id 순."""
    if not kb.drafts_dir.is_dir():
        return [], []
    pending: list[StoredDraft] = []
    corrupt: list[str] = []
    for path in sorted(kb.drafts_dir.glob("*.json")):
        if not DRAFT_ID_RE.fullmatch(path.stem):
            corrupt.append(path.stem)
            continue
        try:
            pending.append(load_stored(path))
        except KBError:
            corrupt.append(path.stem)
    pending.sort(key=lambda d: (d.created_at, d.id))
    return pending, corrupt


def require_draft_id(draft_id: str) -> None:
    if not DRAFT_ID_RE.fullmatch(draft_id):
        raise KBError("invalid_draft_id", f"초안 id 형식이 잘못됐다: {draft_id!r}")


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)
