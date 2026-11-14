"""kb_status — 원본별 파일 수·상태별 수·실패 목록·마지막 스캔 시각."""

from __future__ import annotations

from wiki_mcp.kb.store import KnowledgeBase


def kb_status(kb: KnowledgeBase) -> dict:
    kb.require_initialized()
    sources = kb.load_sources()
    entries = kb.load_manifest().get("entries", [])

    per_source: dict[str, dict] = {}
    for src in sources:
        per_source[src.get("id", "")] = {
            "id": src.get("id", ""),
            "path": src.get("path", ""),
            "files": 0,
            "by_status": {},
            "failed": [],
        }

    for e in entries:
        sid = e.get("source_id", "")
        bucket = per_source.get(sid)
        if bucket is None:  # 등록이 사라진 원본의 잔여 항목
            continue
        status = e.get("status", "")
        bucket["files"] += 1
        bucket["by_status"][status] = bucket["by_status"].get(status, 0) + 1
        if status == "failed":
            bucket["failed"].append(
                {
                    "rel_path": e.get("rel_path", ""),
                    "reason": e.get("reason"),
                }
            )

    last = kb.load_last_scan()
    return {
        "sources": list(per_source.values()),
        "last_scan": (
            {"scanned_at": last.get("scanned_at"), "results": last.get("results")} if last else None
        ),
    }
