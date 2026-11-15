"""mark_orphans — 원본 소실 표시(결정적)·scan 자동 실행 (설계 3.3절, O1~O7).

공통: 승인된 노트 1개(원본 1개와 연결). 원본 폴더는 절대 쓰지 않는다.
"""

from __future__ import annotations

import frontmatter
import pytest
from kb_helper import DRAFT_NOW, future_now

from wiki_mcp.kb.drafts import DraftInput, EvidenceInput, approve_draft, save_draft
from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.orphans import mark_orphans
from wiki_mcp.kb.scanner import scan_sources
from wiki_mcp.kb.sources import add_source

QUOTE = "국내 출장 일비는 하루 2만 원으로 한다"
NOTE_ID = "travel-expense-rule"


def _now():
    return lambda: DRAFT_NOW


@pytest.fixture
def linked(kb, tmp_path):
    """원본 1개를 근거로 노트를 승인해 links 를 만든다. (docs, file) 반환."""
    docs = tmp_path / "원본 자료"
    docs.mkdir()
    f = docs / "규정.md"
    f.write_text(f"# 출장 규정\n\n{QUOTE}.\n", encoding="utf-8")
    add_source(kb, docs)
    scan_sources(kb, now=future_now())
    entry = kb.load_manifest()["entries"][0]
    r = save_draft(
        kb,
        DraftInput(
            kind="new",
            note_id=NOTE_ID,
            title="출장비 기준",
            body="본문.",
            evidence=[
                EvidenceInput(source_id=entry["source_id"], rel_path=entry["rel_path"], quote=QUOTE)
            ],
        ),
        actor="mcp",
        now=_now(),
        token=lambda: "a1b2c3",
    )
    approve_draft(kb, r["draft"]["id"], actor="cli", now=_now())
    return docs, f


def note_path(kb):
    return kb.wiki_dir / "concepts" / f"{NOTE_ID}.md"


def current_status(kb) -> str | None:
    return frontmatter.load(note_path(kb)).get("kb_source_status")


def drop_status_line(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("kb_source_status:"))


class TestMarkOrphans:
    def test_o1_deleted_source_marks_missing(self, kb, tmp_path, linked):
        docs, f = linked
        before = note_path(kb).read_text(encoding="utf-8")
        f.unlink()
        result = scan_sources(kb, now=future_now())
        assert result["orphans"]["marked_missing"] == [NOTE_ID]
        assert current_status(kb) == "missing"
        after = note_path(kb).read_text(encoding="utf-8")
        assert after  # 노트는 지우지 않는다
        assert drop_status_line(after).strip() == drop_status_line(before).strip()

    def test_o2_changed_source_marks_changed(self, kb, tmp_path, linked):
        docs, f = linked
        f.write_text(f"# 규정\n\n{QUOTE} — 개정.\n", encoding="utf-8")
        result = scan_sources(kb, now=future_now())
        assert result["orphans"]["marked_changed"] == [NOTE_ID]
        assert current_status(kb) == "changed"

    def test_o3_restored_source_clears_flag(self, kb, tmp_path, linked):
        docs, f = linked
        original = f.read_bytes()
        f.write_text(f"# 규정\n\n{QUOTE} — 개정.\n", encoding="utf-8")
        scan_sources(kb, now=future_now())
        assert current_status(kb) == "changed"
        # 원래 내용 복원 — sha 가 같아지면 해제
        f.write_bytes(original)
        result = scan_sources(kb, now=future_now())
        assert NOTE_ID in result["orphans"]["cleared"]
        assert current_status(kb) is None
        # 지웠던 파일이 같은 내용으로 다시 생겨도 해제된다
        f.unlink()
        scan_sources(kb, now=future_now())
        assert current_status(kb) == "missing"
        f.write_bytes(original)
        result = scan_sources(kb, now=future_now())
        assert NOTE_ID in result["orphans"]["cleared"]
        assert current_status(kb) is None

    def test_o4_idempotent_second_call_writes_nothing(self, kb, tmp_path, linked):
        docs, f = linked
        f.unlink()
        scan_sources(kb, now=future_now())
        first_stat = note_path(kb).stat()
        first_bytes = note_path(kb).read_bytes()
        out2 = mark_orphans(kb)
        assert (
            out2["marked_missing"] == [] and out2["marked_changed"] == [] and out2["cleared"] == []
        )
        assert note_path(kb).read_bytes() == first_bytes
        assert note_path(kb).stat().st_mtime == first_stat.st_mtime

    def test_o5_missing_source_root_carries_over(self, kb, tmp_path, linked):
        docs, f = linked
        renamed = tmp_path / "원본 자료 (옮김)"
        docs.rename(renamed)
        result = scan_sources(kb, now=future_now())
        assert any(w["code"] == "source_missing" for w in result["warnings"])
        orphans = result["orphans"]
        assert (
            orphans["marked_missing"] == []
            and orphans["marked_changed"] == []
            and orphans["cleared"] == []
        )
        assert current_status(kb) is None

    def test_o6_note_missing_is_skipped(self, kb, tmp_path, linked):
        docs, f = linked
        note_path(kb).unlink()
        out = mark_orphans(kb)
        assert out["skipped"] == [{"note_id": NOTE_ID, "reason": "note_missing"}]
        # 표시가 필요한데 머리말이 없는 노트는 note_frontmatter_unsupported 로 건너뛴다
        p = note_path(kb)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("머리말 없는 본문.\n", encoding="utf-8")
        f.unlink()  # 표시가 필요해진다(scan 을 돌면 자동 실행되니 manifest 만 고친다)
        manifest = kb.load_manifest()
        manifest["entries"] = [e for e in manifest["entries"] if e["rel_path"] != "규정.md"]
        kb.save_manifest(manifest)
        out2 = mark_orphans(kb)
        assert out2["skipped"] == [{"note_id": NOTE_ID, "reason": "note_frontmatter_unsupported"}]

    def test_o7_corrupt_links_fails_scan_gracefully(self, kb, tmp_path, linked):
        docs, f = linked
        kb.links_file.write_text("{ 깨진 links", encoding="utf-8")
        result = scan_sources(kb, now=future_now())
        assert result["orphans"] is None
        assert {"code": "mark_orphans_failed", "detail": "corrupt_links"} in result["warnings"]

    def test_checked_counts_and_ordering(self, kb, tmp_path, linked):
        out = mark_orphans(kb)
        assert out["checked"] == 1
        assert out["unchanged"] == 1
        assert out["marked_missing"] == [] and out["marked_changed"] == [] and out["cleared"] == []
        assert out["skipped"] == []

    def test_d19_busy_lock(self, kb, tmp_path, linked):
        kb.meta_dir.mkdir(parents=True, exist_ok=True)
        lock = kb.meta_dir / "kb.lock"
        lock.write_text("1", encoding="ascii")
        try:
            with pytest.raises(KBError) as e:
                mark_orphans(kb, lock_timeout=0.2)
            assert e.value.code == "kb_busy"
        finally:
            lock.unlink(missing_ok=True)


# ─── 0.4.0 리뷰 반영 — 노트별 예외 격리(코드 4) ────────────────────────
class TestUnreadableNotes:
    def test_unreadable_note_is_skipped(self, kb, tmp_path, linked):
        """cp949 등으로 읽을 수 없는 노트는 note_unreadable 로 건너뛴다(죽지 않는다)."""
        docs, f = linked
        f.unlink()
        note_path(kb).write_bytes(
            "---\r\nid: travel-expense-rule\r\n---\r\n국내 출장 일비는...".encode("cp949")
        )
        result = scan_sources(kb, now=future_now())  # 예외 없이 성공해야 한다
        assert result["orphans"] is not None
        assert {"note_id": NOTE_ID, "reason": "note_unreadable"} in result["orphans"]["skipped"]

    def test_scan_survives_unexpected_mark_orphans_failure(self, kb, tmp_path, linked, monkeypatch):
        """mark_orphans 의 예상 밖 예외도 경고로 낮춘다 — scan 은 성공(0)."""
        from wiki_mcp.kb import orphans as orphans_mod

        def boom(kb, **kwargs):
            raise RuntimeError("예상 밖 오류")

        monkeypatch.setattr(orphans_mod, "mark_orphans", boom)
        result = scan_sources(kb, now=future_now())
        assert result["orphans"] is None
        assert {"code": "mark_orphans_failed", "detail": "internal"} in result["warnings"]

    def test_crlf_note_keeps_crlf_after_marking(self, kb, tmp_path, linked):
        """CRLF 노트에 표시를 붙여도 줄끝이 바뀌지 않는다."""
        docs, f = linked
        raw = note_path(kb).read_bytes()
        assert b"\r\n" not in raw  # 승인 노트는 LF 다 — CRLF 로 바꿔 들어간다
        note_path(kb).write_bytes(raw.replace(b"\n", b"\r\n"))
        f.unlink()
        result = scan_sources(kb, now=future_now())
        assert result["orphans"]["marked_missing"] == [NOTE_ID]
        after = note_path(kb).read_bytes()
        assert b"\r\n" in after
        assert after.count(b"\n") == after.count(b"\r\n")
