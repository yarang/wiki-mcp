"""반영 이력 — .kb/history.jsonl (append-only, 설계 2.4절).

한 줄에 JSON 하나, UTF-8, ensure_ascii=False. seq 는 파싱되는 줄 중 최대
seq + 1. 파싱 안 되는 줄(쓰다 끊긴 마지막 줄)은 읽을 때 건너뛰고 개수만
corrupt_lines 로 센다. 본문과 인용은 이력에 넣지 않는다 — 결정된 초안 파일을
지우므로 반려된 초안의 문서 발췌가 KB 에 남지 않게 한다.
"""

from __future__ import annotations

import json
import os
import sys

from wiki_mcp.kb.store import KnowledgeBase

HISTORY_LIMIT = 500
# 이력은 사람이 검토하는 규모다. 이보다 크면 경고만 남긴다(읽기·seq 계산은
# 그대로 전체 파일 기준 — 정확성이 먼저다).
HISTORY_SIZE_WARN_BYTES = 5 * 1024 * 1024


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _read_lines(kb: KnowledgeBase) -> tuple[list[dict], int]:
    """(파싱된 레코드 목록, 손상 줄 수). 파일이 없으면 ([], 0).

    errors="replace" — 비UTF-8 바이트가 섞여도 예외 대신 교체 문자로 읽어
    그 줄만 corrupt 로 센다(승인·반려가 막히지 않게).
    """
    if not kb.history_file.exists():
        return [], 0
    if kb.history_file.stat().st_size > HISTORY_SIZE_WARN_BYTES:
        _log(
            f"⚠️ 이력 파일이 {HISTORY_SIZE_WARN_BYTES // (1024 * 1024)}MB를 넘는다: {kb.history_file}"
        )
    records: list[dict] = []
    corrupt = 0
    for line in kb.history_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        if isinstance(data, dict):
            records.append(data)
        else:
            corrupt += 1
    return records, corrupt


def _max_seq(records: list[dict]) -> int:
    best = 0
    for r in records:
        seq = r.get("seq")
        if isinstance(seq, int) and seq > best:
            best = seq
    return best


def _needs_leading_newline(path) -> bool:
    """끝 바이트가 \\n 이 아니면 True.

    쓰다 끊긴 반쪽 줄 뒤에 그대로 이어 붙이면 새 레코드가 그 줄에 합쳐져
    파싱에 실패한다 — 방금 쓴 승인·반려 기록이 사라지는 일을 막는다.
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        with open(path, "rb") as fb:
            fb.seek(-1, os.SEEK_END)
            return fb.read(1) != b"\n"
    except OSError:
        return False


def append_history(kb: KnowledgeBase, record: dict) -> int:
    """이력에 한 줄 append 하고 seq 를 돌려준다.

    kb_lock 보유 상태에서만 호출한다(공개 함수 save/approve/reject 가 잡는다).
    flush + fsync 로 프로세스가 죽어도 줄 단위 무결성을 지킨다.
    """
    records, _corrupt = _read_lines(kb)
    payload = dict(record)
    payload["seq"] = _max_seq(records) + 1
    kb.history_file.parent.mkdir(parents=True, exist_ok=True)
    with open(kb.history_file, "a", encoding="utf-8") as fh:
        if _needs_leading_newline(kb.history_file):
            fh.write("\n")
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return payload["seq"]


def list_history(kb: KnowledgeBase, *, limit: int = 50, offset: int = 0) -> dict:
    """이력 조회 — 최신 것이 앞. limit 은 1~500 로 잘라 맞춘다."""
    kb.require_initialized()
    limit = max(1, min(int(limit), HISTORY_LIMIT))
    offset = max(0, int(offset))
    records, corrupt = _read_lines(kb)
    records.sort(
        key=lambda r: (r.get("seq") if isinstance(r.get("seq"), int) else 0, str(r.get("at", "")))
    )
    newest_first = list(reversed(records))
    return {
        "entries": newest_first[offset : offset + limit],
        "total": len(records),
        "limit": limit,
        "offset": offset,
        "corrupt_lines": corrupt,
    }


def find_decision(kb: KnowledgeBase, draft_id: str) -> dict | None:
    """draft_id 의 마지막 결정(approved|rejected) 줄. 없으면 None."""
    kb.require_initialized()
    records, _corrupt = _read_lines(kb)
    found: dict | None = None
    for r in records:
        if r.get("draft_id") == draft_id and r.get("action") in ("approved", "rejected"):
            found = r
    return found
