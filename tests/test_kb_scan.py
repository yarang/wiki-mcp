"""scan_sources — 결정적 스캔·원본 보존 계약.

핵심 보증: 원본 폴더의 파일 목록·내용·해시·mtime 이 스캔 전후로 같고,
같은 상태의 재스캔은 추출을 다시 하지 않는다(extracted 0).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
from hwpx_helper import make_hwpx, p, sec, t
from kb_helper import future_now

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.scanner import scan_sources
from wiki_mcp.kb.sources import add_source


def _hwpx(path: Path, *paras: str) -> Path:
    return make_hwpx(path, {"Contents/section0.xml": sec("".join(p(t(x)) for x in paras))})


def _snapshot(root: Path) -> dict[str, tuple[str, float, int]]:
    """원본 무결성 스냅샷 — (sha256, mtime, size)."""
    snap: dict[str, tuple[str, float, int]] = {}
    for f in sorted(root.rglob("*")):
        if f.is_file():
            st = f.stat()
            snap[str(f.relative_to(root))] = (
                hashlib.sha256(f.read_bytes()).hexdigest(),
                st.st_mtime,
                st.st_size,
            )
    return snap


@pytest.fixture
def scanned(kb, source_dir):
    """원본 하나(hwp+md)를 등록하고 스캔까지 마친 KB."""
    add_source(kb, source_dir)
    _hwpx(source_dir / "report.hwpx", "출장비 정산 기준을 담은 보고서 본문입니다")
    (source_dir / "memo.md").write_text(
        "# 메모\n\n회의에서 정한 예산 배분 기준에 대한 메모다.", encoding="utf-8"
    )
    scan_sources(kb, now=future_now())
    return kb


class TestOriginalPreservation:
    """원본 보존 시험 — 스캔 두 번, 전후 무결성 동일, 두 번째 추출 0."""

    def test_two_scans_leave_original_untouched(self, kb, source_dir):
        add_source(kb, source_dir)
        sub = source_dir / "2026" / "자료"
        sub.mkdir(parents=True)
        _hwpx(sub / "a.hwpx", "하위 폴더의 한글 문서 본문이 여기 들어간다")
        (source_dir / "b.md").write_text(
            "루트 마크다운 문서 본문 — 충분한 길이를 채운다", encoding="utf-8"
        )
        (source_dir / "c.txt").write_text(
            "텍스트 파일 본문 — 이것도 충분한 길이를 채운다", encoding="utf-8"
        )
        before = _snapshot(source_dir)

        first = scan_sources(kb, now=future_now())
        second = scan_sources(kb, now=future_now())

        assert _snapshot(source_dir) == before
        assert first["new"] == 3 and first["extracted"] == 3
        assert second["new"] == 0 and second["extracted"] == 0
        assert second["unchanged"] == 3

    def test_kb_delete_leaves_no_trace_on_original(self, scanned, source_dir, kb_root):
        """KB 폴더 전체를 지워도 원본은 그대로다(흔적 없음)."""
        before = _snapshot(source_dir)
        import shutil

        shutil.rmtree(kb_root)
        assert _snapshot(source_dir) == before


class TestVerdicts:
    def test_changed_file_reextracted(self, kb, scanned, source_dir):
        (source_dir / "memo.md").write_text(
            "# 메모\n\n바뀐 예산 기준 내용으로 수정했다.", encoding="utf-8"
        )
        out = scan_sources(kb, now=future_now())
        assert out["changed"] == 1
        assert out["extracted"] == 1

    def test_deleted_file_removed_from_manifest(self, kb, scanned, source_dir):
        (source_dir / "memo.md").unlink()
        out = scan_sources(kb, now=future_now())
        assert out["deleted"] == 1
        rels = [e["rel_path"] for e in scanned.load_manifest()["entries"]]
        assert "memo.md" not in rels

    def test_deleted_extract_file_cleaned_when_unreferenced(self, kb, scanned, source_dir):
        target = source_dir / "report.hwpx"
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        extract = scanned.extract_path(sha)
        assert extract.exists()
        target.unlink()
        scan_sources(kb, now=future_now())
        assert not extract.exists()

    def test_failed_file_retried_and_recovers(self, kb, source_dir):
        """실패(여선 unsupported) 후 형식을 고쳐 다시 넣으면 회복된다."""
        add_source(kb, source_dir)
        bad = source_dir / "note.hwp"
        bad.write_bytes(b"HWP binary placeholder")
        first = scan_sources(kb, now=future_now())
        assert first["failed"] == 1

        bad.unlink()
        (source_dir / "note.md").write_text(
            "회복된 문서 본문입니다. 형식을 고쳐 다시 넣었다.", encoding="utf-8"
        )
        second = scan_sources(kb, now=future_now())
        assert second["deleted"] == 1
        entry = [e for e in kb.load_manifest()["entries"] if e["rel_path"] == "note.md"][0]
        assert entry["status"] == "ok"


class TestExclusions:
    @pytest.mark.parametrize(
        "name",
        [
            "~$report.docx",
            ".~lock.docx#",
            "draft.tmp",
            "backup.bak",
            "Thumbs.db",
            "desktop.ini",
            ".hidden.md",
        ],
    )
    def test_transient_files_ignored(self, kb, source_dir, name):
        add_source(kb, source_dir)
        (source_dir / name).write_text("임시 파일 내용", encoding="utf-8")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0 and out["failed"] == 0

    def test_hidden_directory_pruned(self, kb, source_dir):
        add_source(kb, source_dir)
        git_dir = source_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config.md").write_text("숨김 폴더 문서", encoding="utf-8")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0

    def test_unknown_extension_ignored_quietly(self, kb, source_dir):
        add_source(kb, source_dir)
        (source_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff")
        (source_dir / "app.exe").write_bytes(b"MZ")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0 and out["failed"] == 0

    def test_hwp_recorded_as_unsupported(self, kb, source_dir):
        add_source(kb, source_dir)
        (source_dir / "legacy.hwp").write_bytes(b"HWP binary")
        out = scan_sources(kb, now=future_now())
        assert out["failed"] == 1
        entry = kb.load_manifest()["entries"][0]
        assert entry["status"] == "failed"
        assert entry["reason"] == "unsupported"
        # 두 번째 스캔 — 같은 상태면 조용하다(변경 목록에 중복으로 쌓이지 않는다)
        again = scan_sources(kb, now=future_now())
        assert again["failed"] == 0
        changes = kb.load_last_scan()["changes"]
        assert changes == []

    def test_too_large_recorded(self, kb, source_dir):
        add_source(kb, source_dir)
        big = source_dir / "big.md"
        big.write_text("가" * 100, encoding="utf-8")
        out = scan_sources(kb, now=future_now(), max_file_size=10)
        assert out["failed"] == 1
        entry = kb.load_manifest()["entries"][0]
        assert entry["reason"] == "too_large"

    def test_unstable_file_skipped_then_picked_up(self, kb, source_dir):
        add_source(kb, source_dir)
        f = source_dir / "writing.md"
        f.write_text("저장 중인 문서 내용입니다. 아직 안정화되지 않았다.", encoding="utf-8")
        # now 를 파일 mtime 과 같은 '지금'으로 두면 unstable 로 건너뛴다
        real_now = time.time()
        first = scan_sources(kb, now=lambda: real_now + 0.5)
        assert first["new"] == 0
        # 시간이 흐른 뒤(또는 안정화된 뒤)에는 잡힌다
        second = scan_sources(kb, now=lambda: real_now + 10)
        assert second["new"] == 1

    def test_include_glob_limits_extraction(self, kb, source_dir):
        add_source(kb, source_dir, include=["*.hwpx"])
        _hwpx(source_dir / "a.hwpx", "한글 문서 본문이 여기 들어간다")
        (source_dir / "b.md").write_text("마크다운은 포함 패턴 밖이다", encoding="utf-8")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 1
        rels = [e["rel_path"] for e in kb.load_manifest()["entries"]]
        assert rels == ["a.hwpx"]

    def test_exclude_glob_skips_matching(self, kb, source_dir):
        add_source(kb, source_dir, exclude=["drafts/*"])
        drafts = source_dir / "drafts"
        drafts.mkdir()
        (drafts / "d.md").write_text("초안 문서 — 이 내용은 충분히 길어야 한다", encoding="utf-8")
        (source_dir / "final.md").write_text(
            "최종 문서 본문 — 이 내용은 충분히 길다", encoding="utf-8"
        )
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 1
        rels = [e["rel_path"] for e in kb.load_manifest()["entries"]]
        assert rels == ["final.md"]

    def test_symlink_escaping_source_skipped(self, kb, source_dir, tmp_path):
        add_source(kb, source_dir)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("원본 밖 문서", encoding="utf-8")
        os.symlink(outside / "secret.md", source_dir / "link.md")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0


class TestDedupAndEncodings:
    def test_same_content_shares_one_extract(self, kb, source_dir):
        add_source(kb, source_dir)
        body = p(t("같은 내용을 가진 두 파일의 공유 추출 검증 문단"))
        make_hwpx(source_dir / "one.hwpx", {"Contents/section0.xml": sec(body)})
        make_hwpx(source_dir / "two.hwpx", {"Contents/section0.xml": sec(body)})
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 2
        assert out["extracted"] == 1  # 추출 하나를 공유
        assert len(list(kb.extract_dir.glob("*.md"))) == 1
        assert all(e["status"] == "ok" for e in kb.load_manifest()["entries"])

    def test_cp949_text_file_extracted(self, kb, source_dir):
        add_source(kb, source_dir)
        legacy = source_dir / "legacy.txt"
        legacy.write_bytes("유물 같은 cp949 문서의 본문입니다.".encode("cp949"))
        scan_sources(kb, now=future_now())
        entry = kb.load_manifest()["entries"][0]
        assert entry["status"] == "ok"
        text = kb.extract_path(entry["sha256"]).read_text(encoding="utf-8")
        assert "cp949 문서" in text

    def test_extract_file_format(self, kb, source_dir):
        add_source(kb, source_dir)
        _hwpx(source_dir / "report.hwpx", "추출 파일 형식 검증용 본문 문단")
        scan_sources(kb, now=future_now())
        entry = kb.load_manifest()["entries"][0]
        raw = kb.extract_path(entry["sha256"]).read_text(encoding="utf-8")
        assert raw.startswith("---")
        for field in (
            "source_id:",
            "rel_path:",
            "sha256:",
            "extractor:",
            "extracted_at:",
            "title:",
        ):
            assert field in raw, field
        assert "<!-- section 0 -->" in raw  # 위치 표지
        import frontmatter

        post = frontmatter.loads(raw)
        assert post["rel_path"] == "report.hwpx"
        assert post["extractor"] == "hwpx"
        assert post["title"] == "report"


class TestBookkeeping:
    def test_last_scan_json(self, scanned):
        last = scanned.load_last_scan()
        assert last["scanned_at"]
        assert last["results"]["new"] == 2
        statuses = {c["status"] for c in last["changes"]}
        assert statuses == {"new"}

    def test_manifest_fields(self, scanned, source_dir):
        entries = scanned.load_manifest()["entries"]
        by_rel = {e["rel_path"]: e for e in entries}
        e = by_rel["report.hwpx"]
        assert e["source_id"].startswith("src-")
        assert set(e) >= {
            "source_id",
            "rel_path",
            "size",
            "mtime",
            "sha256",
            "status",
            "reason",
            "extract_id",
            "title",
            "scanned_at",
        }
        assert e["status"] == "ok" and e["reason"] is None
        assert e["size"] == (source_dir / "report.hwpx").stat().st_size

    def test_nfc_normalized_rel_path(self, kb, source_dir):
        """macOS NFD 파일명도 NFC 키 하나로 매니페스트에 남는다."""
        add_source(kb, source_dir)
        import unicodedata

        nfd_name = unicodedata.normalize("NFD", "한글문서.md")
        (source_dir / nfd_name).write_text("NFD 파일명 문서 본문", encoding="utf-8")
        scan_sources(kb, now=future_now())
        rels = [e["rel_path"] for e in kb.load_manifest()["entries"]]
        assert rels == [unicodedata.normalize("NFC", "한글문서.md")]

    def test_lock_prevents_concurrent_scan(self, scanned):
        scanned.lock_file.parent.mkdir(parents=True, exist_ok=True)
        scanned.lock_file.write_text("123", encoding="utf-8")
        past = time.time() - 1
        os.utime(scanned.lock_file, (past, past))
        with pytest.raises(KBError) as ei:
            scan_sources(kb=scanned, now=future_now())
        assert ei.value.code == "scan_in_progress"

    def test_stale_lock_ignored(self, scanned):
        scanned.lock_file.parent.mkdir(parents=True, exist_ok=True)
        scanned.lock_file.write_text("123", encoding="utf-8")
        stale = time.time() - 700  # 10분 넘음
        os.utime(scanned.lock_file, (stale, stale))
        out = scan_sources(kb=scanned, now=future_now())
        assert out["new"] == 0  # 정상 완료
        assert not scanned.lock_file.exists()  # 잠금은 정리됐다

    def test_scan_of_kb_with_no_sources(self, kb):
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0 and out["extracted"] == 0
