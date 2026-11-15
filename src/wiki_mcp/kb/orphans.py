"""mark_orphans — 연결 원본이 지워지거나 바뀐 노트에 표시(설계 3.3절).

결정적이다. links.json 항목을 manifest 와 대조해 노트 머리말의
kb_source_status 를 "missing"|"changed" 로 붙이거나 지운다. 노트는 지우지
않고, 값이 바뀔 때만 파일을 쓴다(멱등 — 서버 autoindex 재색인 유발 최소화).
scan_sources 가 스캔 끝에 자동으로 부르고, CLI kb mark-orphans·MCP
mark_orphans 도구로도 쓸 수 있다.
"""

from __future__ import annotations

import frontmatter
import yaml

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.notes import find_notes, preserve_newlines, set_frontmatter_key
from wiki_mcp.kb.store import KnowledgeBase, atomic_write_text, kb_lock, rel_key
from wiki_mcp.writer import safe_wiki_path


def verdict_for_link(link: dict, index: dict) -> str:
    """links 항목 하나의 판정: missing | unknown | changed | ok."""
    entry = index.get((link.get("source_id", ""), rel_key(link.get("rel_path", ""))))
    if entry is None:
        return "missing"
    if not entry.get("sha256"):  # too_large·cloud_only·locked — 판단 불가
        return "unknown"
    if entry["sha256"] != link.get("sha256"):
        return "changed"
    return "ok"


def desired_status(link_items: list[dict], index: dict, current: str | None) -> str | None:
    """노트 단위 목표값 — missing > changed > unknown(현재 값 유지) > 해제."""
    states = [verdict_for_link(link, index) for link in link_items]
    if not states:
        return current
    if "missing" in states:
        return "missing"
    if "changed" in states:
        return "changed"
    if "unknown" in states:
        return current
    return None


def mark_orphans(kb: KnowledgeBase, *, lock_timeout: float = 10.0) -> dict:
    kb.require_initialized()
    with kb_lock(kb, timeout=lock_timeout):
        return _mark_locked(kb)


def _mark_locked(kb: KnowledgeBase) -> dict:
    links = kb.load_links().get("links", [])
    index = kb.entry_index(kb.load_manifest())
    notes = find_notes(kb.wiki_dir)

    by_note: dict[str, list[dict]] = {}
    for link in links:
        by_note.setdefault(str(link.get("note_id", "")), []).append(link)

    marked_missing: list[str] = []
    marked_changed: list[str] = []
    cleared: list[str] = []
    skipped: list[dict] = []
    unchanged = 0

    for note_id in sorted(by_note):
        paths = _note_paths(kb, notes, note_id, by_note[note_id])
        if isinstance(paths, str):  # skip 사유
            skipped.append({"note_id": note_id, "reason": paths})
            continue
        try:
            result = _mark_one(kb, note_id, by_note[note_id], paths[0], index)
        except KBError as e:
            if e.code == "note_frontmatter_unsupported":
                skipped.append({"note_id": note_id, "reason": e.code})
                continue
            raise
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            # 노트 하나가 깨져(cp949·손상 YAML·잠긴 파일) 있어도 나머지 표시가
            # 멈추지 않게 한다(코드 리뷰 4).
            skipped.append({"note_id": note_id, "reason": "note_unreadable"})
            continue
        if result is None:
            unchanged += 1
        elif result == "missing":
            marked_missing.append(note_id)
        elif result == "changed":
            marked_changed.append(note_id)
        else:
            cleared.append(note_id)

    return {
        "checked": len(by_note),
        "marked_missing": marked_missing,
        "marked_changed": marked_changed,
        "cleared": cleared,
        "unchanged": unchanged,
        "skipped": skipped,
    }


def _note_paths(
    kb: KnowledgeBase, notes: dict[str, list[str]], note_id: str, items: list[dict]
) -> list[str] | str:
    """노트 경로 목록 — 없으면 skip 사유 문자열(note_missing·note_ambiguous).

    links 의 note_path 로 직접 찾는다 — 머리말이 없어 find_notes 가 담지 못한
    노트도 표시 대상에서 빠뜨리지 않게 한다.
    """
    paths = notes.get(note_id, [])
    if not paths:
        fallback: list[str] = []
        for link in items:
            rel = str(link.get("note_path") or "")
            if not rel:
                continue
            try:
                if safe_wiki_path(kb.wiki_dir, rel).is_file():
                    fallback.append(rel)
            except (ValueError, OSError):
                continue
        if not fallback:
            return "note_missing"
        paths = fallback
    if len(paths) > 1:
        return "note_ambiguous"
    return paths


def _mark_one(
    kb: KnowledgeBase, note_id: str, items: list[dict], rel: str, index: dict
) -> str | None:
    """노트 하나의 표시를 갱신한다. None 이면 변화 없음, 아니면 새 상태."""
    abs_path = safe_wiki_path(kb.wiki_dir, rel)
    # newline="" — 원래 줄끝이 그대로 남은 텍스트로 읽는다(쓰기 때 복원).
    with open(abs_path, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    current = frontmatter.load(abs_path).get("kb_source_status")
    desired = desired_status(items, index, current)
    if desired == current:
        return None
    out = set_frontmatter_key(text, "kb_source_status", f'"{desired}"' if desired else None)
    atomic_write_text(abs_path, preserve_newlines(text, out))
    return desired or "cleared"
