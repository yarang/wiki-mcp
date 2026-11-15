"""초안 검토 대기열 — save/list/show/approve/reject (설계 2.2·3.2·3.5·3.6절).

LLM(MCP)은 검증된 초안을 .kb/drafts/ 에 저장만 한다. wiki 노트는 사람이
앱(CLI drafts-approve)에서 승인할 때 생긴다. 승인·반려은 .kb/history.jsonl 에
append-only 로 남는다. 모든 쓰기는 kb_lock 안에서 원자적으로 일어난다.

입력·저장값 검증은 drafts_validate.py 에 있다(파일 크기·함수 길이 상한).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import yaml

from wiki_mcp.kb.drafts_validate import (
    CURRENT_NOTE_CHARS,
    DRAFT_ID_RE,
    LIST_LIMIT,
    MAX_BODY_CHARS,
    MAX_EVIDENCE,
    MAX_PENDING,
    MAX_REASON_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    Actor,
    DraftInput,
    EvidenceInput,
    StoredDraft,
    StoredEvidence,
    check_reason,
    default_token,
    iter_pending,
    load_stored,
    nfc,
    require_draft_id,
    revalidate_stored,
    utc_now,
    validate_evidence,
    validate_fields,
)
from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.evidence import context_around
from wiki_mcp.kb.history import append_history, find_decision
from wiki_mcp.kb.notes import (
    find_notes,
    note_content_hash,
    preserve_newlines,
    render_sources_section,
    replace_note_body,
    set_frontmatter_key,
)
from wiki_mcp.kb.orphans import desired_status
from wiki_mcp.kb.store import (
    KnowledgeBase,
    atomic_write_text,
    kb_lock,
    rel_key,
)
from wiki_mcp.writer import (
    _build_note_path,
    backup_file,
    build_frontmatter,
    safe_wiki_path,
    validate_domain,
    validate_note_id,
)

__all__ = [
    "CURRENT_NOTE_CHARS",
    "DRAFT_ID_RE",
    "DraftInput",
    "EvidenceInput",
    "LIST_LIMIT",
    "MAX_BODY_CHARS",
    "MAX_EVIDENCE",
    "MAX_PENDING",
    "MAX_REASON_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TITLE_CHARS",
    "StoredDraft",
    "approve_draft",
    "list_drafts",
    "reject_draft",
    "reject_stale_drafts",
    "save_draft",
    "show_draft",
]

# issues 표시 순서(3.2절)와 approve 거부 순서(3.6절)는 다르다.
# note_unreadable — update 대상 노트를 읽지 못한 경우(인코딩·파일 손상 등).
# note_conflict·note_ambiguous 와는 동시에 나오지 않는다(읽어야 비교할 수 있다).
_ISSUES_ORDER = (
    "draft_stale",
    "note_not_found",
    "note_conflict",
    "note_unreadable",
    "note_exists",
    "note_ambiguous",
)
_APPROVE_ORDER = (
    "draft_stale",
    "note_not_found",
    "note_ambiguous",
    "note_unreadable",
    "note_conflict",
    "note_exists",
)


def _log(message: str) -> None:
    print(message, file=sys.stderr)


# ─── 조회 보조 ───────────────────────────────────────────────────────
def evidence_source_state(kb: KnowledgeBase, entry: dict | None, sha256: str) -> str:
    """근거 원본 상태 — current | changed | deleted | not_extracted.

    sha 가 같아도 추출 파일이 없으면 not_extracted 다(show_draft·issues 가
    같은 판정을 쓴다).
    """
    if entry is None:
        return "deleted"
    if entry.get("status") != "ok" or not entry.get("sha256"):
        return "not_extracted"
    if entry["sha256"] != sha256:
        return "changed"
    if not kb.extract_path(entry["sha256"]).exists():
        return "not_extracted"
    return "current"


def _compute_issues(
    kb: KnowledgeBase,
    stored: StoredDraft,
    notes: dict[str, list[str]],
    index: dict,
) -> list[str]:
    """대기 초안의 차단 사유(조회 시 계산 — 저장하지 않는다)."""
    found: set[str] = set()
    for ev in stored.evidence:
        entry = index.get((ev.source_id, rel_key(ev.rel_path)))
        if evidence_source_state(kb, entry, ev.sha256) != "current":
            found.add("draft_stale")
            break
    paths = notes.get(stored.note_id, [])
    if stored.kind == "update":
        if not paths:
            found.add("note_not_found")
        elif len(paths) > 1:
            found.add("note_ambiguous")
        else:
            # 대상 노트를 읽지 못하면 목록·일괄 반려가 멈추지 않게 차단 사유로
            # 돌린다(N5) — note_conflict 판정도 이 뒤에만 한다.
            try:
                text = (kb.wiki_dir / paths[0]).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as e:
                _log(f"⚠️ update 대상 노트 읽기 실패: {stored.note_id} ({type(e).__name__})")
                found.add("note_unreadable")
            else:
                if note_content_hash(text) != (stored.base or {}).get("sha256"):
                    found.add("note_conflict")
    else:
        if paths:
            found.add("note_exists")
        else:
            # 저장과 승인 사이에 같은 경로에 머리말 없는 파일이 생겼을 수 있다 —
            # find_notes 가 못 보는 파일도 덮어쓰지 않게 한다(코드 리뷰 3).
            try:
                rel = _build_note_path(stored.domain or "concepts", stored.note_id)
                if safe_wiki_path(kb.wiki_dir, rel).exists():
                    found.add("note_exists")
            except (ValueError, OSError):
                found.add("note_exists")  # 경로를 못 만드는 상태면 승인도 못 한다
    return [code for code in _ISSUES_ORDER if code in found]


def _summary(stored: StoredDraft, issues: list[str]) -> dict:
    sources: list[dict] = []
    for ev in stored.evidence:
        item = {"source_id": ev.source_id, "rel_path": ev.rel_path}
        if item not in sources:
            sources.append(item)
    return {
        "id": stored.id,
        "kind": stored.kind,
        "note_id": stored.note_id,
        "title": stored.title,
        "domain": stored.domain,
        "summary": stored.summary,
        "created_at": stored.created_at,
        "created_by": stored.created_by,
        "evidence_count": len(stored.evidence),
        "sources": sources,
        "issues": issues,
        "approvable": not issues,
    }


def _sources_triple(items) -> list[dict]:
    """{"source_id","rel_path","sha256"} 목록 — 이력·kb_sources 가 함께 쓰는 형태.

    초근거 모델(StoredEvidence)과 links 항목(dict)을 모두 받는다.
    """
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict):
            out.append(
                {"source_id": it["source_id"], "rel_path": it["rel_path"], "sha256": it["sha256"]}
            )
        else:
            out.append({"source_id": it.source_id, "rel_path": it.rel_path, "sha256": it.sha256})
    return out


# ─── 저장 (3.5절) ────────────────────────────────────────────────────
def save_draft(
    kb: KnowledgeBase,
    data: DraftInput,
    *,
    actor: Actor,
    now: Callable[[], datetime] = utc_now,
    token: Callable[[], str] = default_token,
    max_pending: int = MAX_PENDING,
    lock_timeout: float = 10.0,
) -> dict:
    """검증을 통과한 초안을 .kb/drafts/<id>.json 에 저장한다. wiki 는 안 바뀐다."""
    kb.require_initialized()
    validate_fields(data)  # 순수 검증 — 잠금 밖에서 먼저 거른다(긴 입력 방어)
    with kb_lock(kb, timeout=lock_timeout):
        return _save_locked(kb, data, actor=actor, now=now, token=token, max_pending=max_pending)


def _validated_note_id(note_id: str) -> str:
    try:
        return validate_note_id(note_id)  # 2) 노트 id
    except ValueError as e:
        raise KBError("invalid_note_id", str(e)) from e


def _save_locked(
    kb: KnowledgeBase,
    data: DraftInput,
    *,
    actor: Actor,
    now: Callable[[], datetime],
    token: Callable[[], str],
    max_pending: int,
) -> dict:
    note_id = _validated_note_id(data.note_id)
    notes = find_notes(kb.wiki_dir)
    if data.kind == "new":  # 3) 도메인
        stored_domain, stored_tags, target = _prepare_new(kb, data, note_id)
        base, stored_title = None, data.title
    else:
        if data.title or data.tags or data.domain:
            raise KBError(
                "invalid_draft", "update 초안은 본문만 바꾼다 — title/tags/domain 은 비워 둔다"
            )
        stored_domain, stored_tags, target = "", [], None
        base, stored_title = _update_base(kb, notes, note_id)

    pending, _corrupt = _iter_pending_and_check_limit(kb, max_pending)
    if data.kind == "new":
        _reject_new_conflicts(notes, pending, note_id, target)

    index = kb.entry_index(kb.load_manifest())
    evidence = [  # 8) 근거 검증
        validate_evidence(kb, ev, index, i + 1) for i, ev in enumerate(data.evidence)
    ]

    ts = now().astimezone(timezone.utc)  # 9) 저장 — id 와 created_at 이 같은 시각
    stored = _build_stored(
        data,
        note_id,
        stored_title,
        stored_domain,
        stored_tags,
        base,
        evidence,
        actor,
        ts,
        token,
        kb,
    )
    payload = {"schema": 1, **stored.model_dump()}
    kb.drafts_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        kb.drafts_dir / f"{stored.id}.json", json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return {"draft": _summary(stored, [])}


def _prepare_new(kb: KnowledgeBase, data: DraftInput, note_id: str) -> tuple[str, list[str], Path]:
    """new 초안의 저장값(도메인·태그)과 대상 경로. 실패 시 invalid_domain."""
    try:
        stored_domain = validate_domain(data.domain) if data.domain else "concepts"
        target = safe_wiki_path(kb.wiki_dir, _build_note_path(stored_domain, note_id))
    except ValueError as e:
        raise KBError("invalid_domain", str(e)) from e
    return stored_domain, list(data.tags), target


def _iter_pending_and_check_limit(
    kb: KnowledgeBase, max_pending: int
) -> tuple[list[StoredDraft], list[str]]:
    pending, corrupt = iter_pending(kb)
    if len(pending) >= max_pending:  # 5) 대기 상한
        raise KBError("draft_limit_reached", f"대기 초안이 {max_pending}건 상한에 닿았다")
    return pending, corrupt


def _reject_new_conflicts(
    notes: dict[str, list[str]], pending: list[StoredDraft], note_id: str, target: Path | None
) -> None:
    if notes.get(note_id) or (target is not None and target.exists()):  # 6) 이미 있는 노트
        raise KBError("note_exists", f"노트 id 가 이미 있다: {note_id}")
    for p in pending:
        if p.kind == "new" and p.note_id == note_id:
            raise KBError("draft_duplicate", f"같은 note_id 의 new 초안이 대기 중이다: {note_id}")


def _update_base(kb: KnowledgeBase, notes: dict[str, list[str]], note_id: str) -> tuple[dict, str]:
    """update 대상 노트 확인 — (base, 표시용 title). 7단계."""
    paths = notes.get(note_id, [])
    if not paths:
        raise KBError("note_not_found", f"업데이트 대상 노트가 없다: {note_id}")
    if len(paths) > 1:
        raise KBError("note_ambiguous", f"같은 id 의 노트가 둘 이상이다: {note_id}")
    note_file = kb.wiki_dir / paths[0]
    text = note_file.read_text(encoding="utf-8")
    base = {"note_path": paths[0], "sha256": note_content_hash(text)}
    try:
        stored_title = str(frontmatter.load(note_file).get("title") or "")
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        _log(
            f"⚠️ update 대상 노트 title 읽기 실패(빈 제목으로 둔다): {note_file} ({type(e).__name__})"
        )
        stored_title = ""
    return base, stored_title


def _build_stored(
    data: DraftInput,
    note_id: str,
    stored_title: str,
    stored_domain: str,
    stored_tags: list[str],
    base: dict | None,
    evidence: list[StoredEvidence],
    actor: Actor,
    ts: datetime,
    token: Callable[[], str],
    kb: KnowledgeBase,
) -> StoredDraft:
    """초안 id(ts+token, 충돌 시 5회 재시도)를 만들고 저장 모델을 완성한다."""
    for _ in range(5):
        draft_id = f"d-{ts:%Y%m%d}-{ts:%H%M%S}-{token().lower()}"
        if not DRAFT_ID_RE.fullmatch(draft_id):
            raise KBError("internal", "초안 id 토큰이 형식에 맞지 않다")
        if not (kb.drafts_dir / f"{draft_id}.json").exists():
            break
    else:
        raise RuntimeError("초안 id 충돌 — 다시 시도한다")
    return StoredDraft(
        id=draft_id,
        kind=data.kind,
        note_id=note_id,
        title=stored_title,
        domain=stored_domain,
        tags=stored_tags,
        summary=data.summary,
        body=data.body,
        created_at=ts.isoformat(),
        created_by=actor,
        base=base,
        evidence=evidence,
    )


# ─── 목록·상세 (3.2절) ───────────────────────────────────────────────
def list_drafts(kb: KnowledgeBase, *, limit: int = 50, offset: int = 0) -> dict:
    """대기 초안 목록 — created_at 오름차순(FIFO). limit 은 1~200 로 맞춘다."""
    kb.require_initialized()
    limit = max(1, min(int(limit), LIST_LIMIT))
    offset = max(0, int(offset))
    pending, corrupt = iter_pending(kb)
    notes = find_notes(kb.wiki_dir)
    index = kb.entry_index(kb.load_manifest())
    drafts = [_summary(d, _compute_issues(kb, d, notes, index)) for d in pending]
    return {
        "drafts": drafts[offset : offset + limit],
        "total": len(drafts),
        "limit": limit,
        "offset": offset,
        "max_pending": MAX_PENDING,
        "corrupt_drafts": corrupt,
    }


def show_draft(kb: KnowledgeBase, draft_id: str, *, context_chars: int = 600) -> dict:
    """초안 상세 — 본문·인용·문맥·현재 노트를 가공 없이 돌려준다(JSON 직렬화만)."""
    kb.require_initialized()
    require_draft_id(draft_id)
    context_chars = max(0, min(int(context_chars), 2000))
    path = kb.drafts_dir / f"{draft_id}.json"
    if not path.exists():
        return _missing_draft(kb, draft_id)
    stored = load_stored(path)
    notes = find_notes(kb.wiki_dir)
    index = kb.entry_index(kb.load_manifest())
    return {
        "draft": {
            **_summary(stored, _compute_issues(kb, stored, notes, index)),
            "tags": list(stored.tags),
            "body": stored.body,
            "base": stored.base,
            "evidence": _evidence_details(kb, stored, index, context_chars),
            "current_note": _current_note(kb, stored, notes),
        }
    }


def _missing_draft(kb: KnowledgeBase, draft_id: str) -> dict:
    if find_decision(kb, draft_id) is not None:
        raise KBError("draft_already_decided", f"이미 결정된 초안이다: {draft_id}")
    raise KBError("draft_not_found", f"대기 중인 초안이 없다: {draft_id}")


def _evidence_details(
    kb: KnowledgeBase, stored: StoredDraft, index: dict, context_chars: int
) -> list[dict]:
    sources = {s.get("id"): s for s in kb.load_sources()}
    out: list[dict] = []
    for i, ev in enumerate(stored.evidence):
        entry = index.get((ev.source_id, rel_key(ev.rel_path)))
        state = evidence_source_state(kb, entry, ev.sha256)
        context = None
        if state == "current":
            extract_file = kb.extract_path(entry["sha256"])
            nfc_text = nfc(frontmatter.load(extract_file).content)
            context = context_around(nfc_text, ev.offset_start, ev.offset_end, context_chars)
        src = sources.get(ev.source_id)
        source_path = None
        if src is not None and entry is not None and src.get("path"):
            fs_rel = entry.get("fs_rel_path") or entry.get("rel_path", "")
            source_path = str(Path(str(src["path"])) / Path(*Path(fs_rel).parts))
        out.append(
            {
                "index": i,
                "source_id": ev.source_id,
                "rel_path": ev.rel_path,
                "sha256": ev.sha256,
                "quote": ev.quote,
                "location": ev.location,
                "offset_start": ev.offset_start,
                "offset_end": ev.offset_end,
                "source_state": state,
                "source_path": source_path,
                "context": context,
            }
        )
    return out


def _current_note(
    kb: KnowledgeBase, stored: StoredDraft, notes: dict[str, list[str]]
) -> dict | None:
    if stored.kind != "update":
        return None
    paths = notes.get(stored.note_id, [])
    if len(paths) != 1:
        return None
    try:
        text = (kb.wiki_dir / paths[0]).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        # 읽지 못한 노트 내용은 비운다 — issues 의 note_unreadable 이 알린다(N5).
        _log(
            f"⚠️ update 대상 노트 읽기 실패(current_note 생략): {stored.note_id} ({type(e).__name__})"
        )
        return None
    return {
        "path": paths[0],
        "content": text[:CURRENT_NOTE_CHARS],
        "truncated": len(text) > CURRENT_NOTE_CHARS,
    }


# ─── 승인·반려 공통 (3.6절 1~4단계) ───────────────────────────────────
def _load_pending_for_decision(
    kb: KnowledgeBase, draft_id: str, *, corrupt_ok: bool
) -> tuple[Path, StoredDraft | None]:
    """결정 대기 초안 로드 — 파일·이력 동시 존재 정리와 없음 판정을 공유한다.

    corrupt_ok=True(reject)면 손상 파일도 None 으로 돌려준다.
    """
    path = kb.drafts_dir / f"{draft_id}.json"
    decided = find_decision(kb, draft_id)
    if path.exists() and decided is not None:  # 지난 크래시 정리
        path.unlink()
        raise KBError(
            "draft_already_decided",
            f"이미 {decided.get('action')}된 초안이다: {draft_id} — 남은 파일을 치웠다",
        )
    if not path.exists():
        if decided is not None:
            raise KBError(
                "draft_already_decided", f"이미 {decided.get('action')}된 초안이다: {draft_id}"
            )
        raise KBError("draft_not_found", f"대기 중인 초안이 없다: {draft_id}")
    try:
        return path, load_stored(path)
    except KBError:
        if corrupt_ok:
            return path, None
        raise


def _reject_record(
    kb: KnowledgeBase,
    stored: StoredDraft | None,
    draft_id: str,
    *,
    actor: Actor,
    reason: str,
    now: Callable[[], datetime],
) -> int:
    """이력 append + 초안 파일 삭제. kb_lock 보유 상태에서만 호출한다."""
    seq = append_history(
        kb,
        {
            "at": now().isoformat(),
            "action": "rejected",
            "actor": actor,
            "draft_id": draft_id,
            "kind": stored.kind if stored else None,
            "note_id": stored.note_id if stored else None,
            "title": stored.title if stored else None,
            "path": None,
            "reason": reason,
            "sources": _sources_triple(stored.evidence) if stored else [],
            "evidence_count": len(stored.evidence) if stored else 0,
        },
    )
    (kb.drafts_dir / f"{draft_id}.json").unlink(missing_ok=True)
    return seq


def reject_draft(
    kb: KnowledgeBase,
    draft_id: str,
    *,
    actor: Actor,
    reason: str = "",
    now: Callable[[], datetime] = utc_now,
    lock_timeout: float = 10.0,
) -> dict:
    """대기 초안을 반려한다. 손상된 초안도 반려할 수 있다(이력에 null 로 기록)."""
    kb.require_initialized()
    require_draft_id(draft_id)
    check_reason(reason)
    with kb_lock(kb, timeout=lock_timeout):
        _path, stored = _load_pending_for_decision(kb, draft_id, corrupt_ok=True)
        seq = _reject_record(kb, stored, draft_id, actor=actor, reason=reason, now=now)
        return {"rejected": draft_id, "history_seq": seq}


def reject_stale_drafts(
    kb: KnowledgeBase,
    *,
    actor: Actor,
    reason: str = "",
    now: Callable[[], datetime] = utc_now,
    lock_timeout: float = 10.0,
) -> dict:
    """issues 에 draft_stale 이 있는 대기 초안을 모두 반려한다(대기열 선점 해소).

    결과: {"rejected": [id…], "history_seqs": [seq…]} — created_at 순서.
    """
    kb.require_initialized()
    check_reason(reason)
    with kb_lock(kb, timeout=lock_timeout):
        pending, _corrupt = iter_pending(kb)
        notes = find_notes(kb.wiki_dir)
        index = kb.entry_index(kb.load_manifest())
        rejected: list[str] = []
        seqs: list[int] = []
        for stored in pending:
            issues = _compute_issues(kb, stored, notes, index)
            if "draft_stale" not in issues:
                continue
            seqs.append(_reject_record(kb, stored, stored.id, actor=actor, reason=reason, now=now))
            rejected.append(stored.id)
        return {"rejected": rejected, "history_seqs": seqs}


# ─── 승인 (3.6절) ────────────────────────────────────────────────────
def approve_draft(
    kb: KnowledgeBase,
    draft_id: str,
    *,
    actor: str,
    now: Callable[[], datetime] = utc_now,
    lock_timeout: float = 10.0,
) -> dict:
    """대기 초안을 승인해 노트를 만들고(또는 갱신하고) links·이력을 남긴다."""
    kb.require_initialized()
    require_draft_id(draft_id)
    with kb_lock(kb, timeout=lock_timeout):
        return _approve_locked(kb, draft_id, actor=actor, now=now)


def _approve_locked(
    kb: KnowledgeBase, draft_id: str, *, actor: Actor, now: Callable[[], datetime]
) -> dict:
    path, stored = _load_pending_for_decision(kb, draft_id, corrupt_ok=False)
    revalidate_stored(stored, draft_id)  # 저장값 재검증 — 변조된 초안 파일 방어

    notes = find_notes(kb.wiki_dir)
    index = kb.entry_index(kb.load_manifest())
    issues = _compute_issues(kb, stored, notes, index)
    first = next((c for c in _APPROVE_ORDER if c in issues), None)
    if first is not None:
        raise KBError(first, f"승인할 수 없다({first}): {stored.note_id} {draft_id}")

    links_data = kb.load_links()
    links = links_data.get("links", [])
    at = now()
    # issues 를 통과했다면 update 대상 노트가 정확히 하나다 — 사용자가 노트를
    # 옮겼어도 그 경로가 승인 대상이다(base.note_path 이 아님).
    note_rel = (
        notes.get(stored.note_id, [""])[0]
        if stored.kind == "update"
        else _build_note_path(stored.domain or "concepts", stored.note_id)
    )
    new_items = _link_items_for_draft(stored, note_rel, at)
    merged, links_updated = _merge_links(links, new_items)
    note_links = [lk for lk in merged if lk.get("note_id") == stored.note_id]
    backup_rel = _write_approved_note(kb, stored, index, draft_id, note_rel, at, note_links)

    kb.save_links({"version": links_data.get("version", 1), "links": merged})
    seq = _append_approval(kb, stored, draft_id, note_rel, actor, at)
    path.unlink()  # 10)
    return {
        "approved": draft_id,
        "kind": stored.kind,
        "note_id": stored.note_id,
        "path": note_rel,
        "links_updated": links_updated,
        "backup": backup_rel,
        "history_seq": seq,
    }


def _append_approval(
    kb: KnowledgeBase, stored: StoredDraft, draft_id: str, note_rel: str, actor: Actor, at: datetime
) -> int:
    return append_history(
        kb,
        {
            "at": at.isoformat(),
            "action": "approved",
            "actor": actor,
            "draft_id": draft_id,
            "kind": stored.kind,
            "note_id": stored.note_id,
            "title": stored.title,
            "path": note_rel,
            "reason": None,
            "sources": _sources_triple(stored.evidence),
            "evidence_count": len(stored.evidence),
        },
    )


def _write_approved_note(
    kb: KnowledgeBase,
    stored: StoredDraft,
    index: dict,
    draft_id: str,
    note_rel: str,
    at: datetime,
    note_links: list[dict],
) -> str | None:
    """노트를 쓰고(7단계) backup 상대 경로를 돌려준다(new 는 None)."""
    abs_path = safe_wiki_path(kb.wiki_dir, note_rel)
    if stored.kind == "update":
        new_text = _update_note_content(stored, abs_path, index, draft_id, at, note_links)
        backup = backup_file(abs_path)  # 계산이 끝난 뒤에 백업한다(실패 시 흔적 없음)
        atomic_write_text(abs_path, new_text)
        return backup.relative_to(kb.wiki_dir).as_posix() if backup else None
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(abs_path, _new_note_content(stored, draft_id, at, note_links))
    return None


def _new_note_content(
    stored: StoredDraft, draft_id: str, at: datetime, new_items: list[dict]
) -> str:
    date_str = at.astimezone().date().isoformat()
    fm = build_frontmatter(
        id=stored.note_id,
        title=stored.title,
        source_url="",
        tags=stored.tags,
        created_at=date_str,
        last_updated=date_str,
        extra={"kb_draft_id": draft_id},
    )
    head, _, tail = fm.rpartition("---\n")
    fm_with_sources = f"{head}kb_sources: {_kb_sources_value(new_items)}\n---\n{tail}"
    return (
        fm_with_sources + "\n" + stored.body.rstrip() + "\n\n" + render_sources_section(new_items)
    )


def _update_note_content(
    stored: StoredDraft,
    abs_path: Path,
    index: dict,
    draft_id: str,
    at: datetime,
    note_links: list[dict],
) -> str:
    """update 승인의 새 노트 텍스트 — 머리말 키 편집·본문 교체·줄끝 보존."""
    date_str = at.astimezone().date().isoformat()
    # newline="" — 원래 줄끝(CRLF)이 그대로 남은 텍스트로 읽는다. 편집은
    # LF 정규화로 하고, 쓰기 직전에 preserve_newlines 로 되돌린다.
    with open(abs_path, "r", encoding="utf-8", newline="") as fh:
        original = fh.read()
    new_text = replace_note_body(
        original, stored.body.rstrip() + "\n\n" + render_sources_section(note_links)
    )
    new_text = set_frontmatter_key(new_text, "last_updated", f'"{date_str}"')
    new_text = set_frontmatter_key(new_text, "kb_draft_id", f'"{draft_id}"')
    new_text = set_frontmatter_key(new_text, "kb_sources", _kb_sources_value(note_links))
    desired = desired_status(note_links, index, frontmatter.load(abs_path).get("kb_source_status"))
    new_text = set_frontmatter_key(
        new_text, "kb_source_status", f'"{desired}"' if desired else None
    )
    return preserve_newlines(original, new_text)


# ─── links 조작 ──────────────────────────────────────────────────────
def _link_items_for_draft(stored: StoredDraft, note_path_rel: str, at: datetime) -> list[dict]:
    """초안 근거를 links 항목((source_id, rel_key)별)으로 묶는다."""
    grouped: dict[tuple[str, str], dict] = {}
    for ev in stored.evidence:
        key = (ev.source_id, rel_key(ev.rel_path))
        item = grouped.get(key)
        if item is None:
            item = {
                "note_id": stored.note_id,
                "note_path": note_path_rel,
                "source_id": ev.source_id,
                "rel_path": ev.rel_path,
                "sha256": ev.sha256,
                "evidence": [],
                "draft_id": stored.id,
                "linked_at": at.isoformat(),
            }
            grouped[key] = item
        item["evidence"].append({"quote": ev.quote, "location": ev.location})
    return [grouped[k] for k in sorted(grouped, key=lambda k: (k[0], grouped[k]["rel_path"]))]


def _merge_links(existing: list[dict], new_items: list[dict]) -> tuple[list[dict], int]:
    """초안이 인용한 파일의 항목은 교체하고, 인용하지 않은 기존 항목은 유지한다."""
    new_keys = {(n["note_id"], n["source_id"], rel_key(n["rel_path"])) for n in new_items}
    out = [
        link
        for link in existing
        if (link.get("note_id"), link.get("source_id"), rel_key(link.get("rel_path", "")))
        not in new_keys
    ]
    out.extend(new_items)
    return out, len(new_items)


def _kb_sources_value(link_items: list[dict]) -> str:
    """머리말 kb_sources 값 — 한 줄 JSON(YAML 흐름 표기로도 읽힌다)."""
    items = sorted(_sources_triple(link_items), key=lambda x: (x["source_id"], x["rel_path"]))
    return json.dumps(items, ensure_ascii=False)
