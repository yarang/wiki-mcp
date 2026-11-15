"""반영 이력 — append_history·list_history·find_decision (설계 2.4절).

이력은 .kb/history.jsonl 한 줄 JSON, append-only. 본문과 인용은 넣지 않는다.
"""

from __future__ import annotations

import json

from kb_helper import DRAFT_NOW

from wiki_mcp.kb.history import append_history, find_decision, list_history
from wiki_mcp.kb.store import kb_lock


def _record(action: str, seq_note: str, **overrides) -> dict:
    base = {
        "at": DRAFT_NOW.isoformat(),
        "action": action,
        "actor": "cli",
        "draft_id": f"d-20261115-12000{seq_note}-a1b2c3",
        "kind": "new",
        "note_id": f"note-{seq_note}",
        "title": "제목",
        "path": f"concepts/note-{seq_note}.md",
        "reason": None,
        "sources": [{"source_id": "src-1", "rel_path": "a.md", "sha256": "0" * 64}],
        "evidence_count": 1,
    }
    base.update(overrides)
    return base


class TestAppendHistory:
    def test_append_writes_line_and_returns_seq(self, kb):
        with kb_lock(kb):
            seq = append_history(kb, _record("approved", "0"))
        assert seq == 1
        line = kb.history_file.read_text(encoding="utf-8").splitlines()[-1]
        assert json.loads(line)["seq"] == 1
        assert json.loads(line)["action"] == "approved"

    def test_seq_increments_per_parsed_lines(self, kb):
        with kb_lock(kb):
            assert append_history(kb, _record("approved", "0")) == 1
            assert append_history(kb, _record("rejected", "1")) == 2


def _with_seq(record: dict, seq: int) -> dict:
    return {"seq": seq, **record}


class TestListHistory:
    def test_empty_history(self, kb):
        out = list_history(kb)
        assert out == {
            "entries": [],
            "total": 0,
            "limit": 50,
            "offset": 0,
            "corrupt_lines": 0,
        }

    def test_newest_first_and_pagination(self, kb):
        with kb_lock(kb):
            for i in range(4):
                append_history(kb, _record("approved", str(i)))
        out = list_history(kb, limit=2, offset=1)
        assert [e["note_id"] for e in out["entries"]] == ["note-2", "note-1"]
        assert out["total"] == 4
        assert out["limit"] == 2
        assert out["offset"] == 1

    def test_limit_clamped_to_500(self, kb):
        assert list_history(kb, limit=9999)["limit"] == 500

    def test_h1_corrupt_half_line_skipped_and_counted(self, kb):
        """H1 — 3줄 뒤 깨진 반쪽 줄: 최신순 3건 + corrupt_lines 1, 다음 seq 는 4."""
        lines = [
            json.dumps(_with_seq(_record("approved", str(i)), i + 1), ensure_ascii=False)
            for i in range(3)
        ]
        kb.history_file.parent.mkdir(parents=True, exist_ok=True)
        kb.history_file.write_text(
            "\n".join(lines) + "\n" + '{"seq": 4, "action": "appro', encoding="utf-8"
        )
        out = list_history(kb)
        assert len(out["entries"]) == 3
        assert out["corrupt_lines"] == 1
        assert out["total"] == 3
        with kb_lock(kb):
            assert append_history(kb, _record("rejected", "9")) == 4


class TestFindDecision:
    def test_finds_latest_decision_for_draft(self, kb):
        with kb_lock(kb):
            append_history(kb, _record("approved", "0"))
        found = find_decision(kb, "d-20261115-120000-a1b2c3")
        assert found is not None
        assert found["action"] == "approved"
        assert found["note_id"] == "note-0"

    def test_unknown_draft_returns_none(self, kb):
        assert find_decision(kb, "d-20261115-120000-zzzzzz") is None


# ─── 0.4.0 리뷰 반영 — append 무결성·인코딩 ────────────────────────────
class TestAppendIntegrity:
    def test_append_after_half_line_starts_new_line(self, kb):
        """끝이 개행 없이 끊긴 파일에 append 하면 새 줄로 붙고 이후 읽힌다."""
        lines = [
            json.dumps(_with_seq(_record("approved", str(i)), i + 1), ensure_ascii=False)
            for i in range(2)
        ]
        kb.history_file.parent.mkdir(parents=True, exist_ok=True)
        kb.history_file.write_text(
            "\n".join(lines) + "\n" + '{"seq": 3, "action": "re', encoding="utf-8"
        )
        with kb_lock(kb):
            seq = append_history(kb, _record("rejected", "5"))
        assert seq == 3
        out = list_history(kb)
        assert out["corrupt_lines"] == 1
        assert len(out["entries"]) == 3  # 온전한 2줄 + 새 레코드
        assert out["entries"][0]["seq"] == 3  # 방금 쓴 레코드가 살아 있다

    def test_append_to_empty_and_missing_file(self, kb):
        kb.history_file.parent.mkdir(parents=True, exist_ok=True)
        with kb_lock(kb):
            assert append_history(kb, _record("approved", "0")) == 1
        kb.history_file.unlink()
        with kb_lock(kb):
            assert append_history(kb, _record("approved", "1")) == 1

    def test_non_utf8_bytes_counted_corrupt_not_raising(self, kb):
        """비UTF-8 바이트가 섞인 반쪽 줄도 읽기가 예외 없이 corrupt 로 센다."""
        with kb_lock(kb):
            append_history(kb, _record("approved", "0"))
        with open(kb.history_file, "ab") as fh:
            fh.write(b'{"seq": 2, "action": "appro\xc3\x28')
        out = list_history(kb)  # UnicodeDecodeError 가 아니라 결과를 돌려준다
        assert out["corrupt_lines"] == 1
        assert out["total"] == 1
        with kb_lock(kb):
            assert append_history(kb, _record("rejected", "1")) == 2
